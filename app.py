"""District of Mount Waddington Access Society website."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import warnings
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from typing import Callable, TypeVar, cast
from urllib.error import HTTPError
from urllib import request as urllib_request

import click
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_migrate import Migrate
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import selectinload
from sqlalchemy import and_, or_, func, select, case
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

from activities import register_activities
from activity_models import anonymize_activity_member
from extensions import db
from messaging_models import MessagePair
from messaging_routes import register_messaging
from messaging_service import (
    MessagingPolicy, blocked_with, forget_network_member, history_access, is_moderator, participating,
)
from permissions import Capability, has_capability, is_active_member, adult_birth_date_cutoff
from registration_service import RegistrationPolicy, detach_member_registrations

migrate = Migrate()
F = TypeVar("F", bound=Callable[..., object])
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 12


class User(db.Model):
    """A member account or a pending membership access request."""

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(40), nullable=True)
    username = db.Column(db.String(40), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def member_number(self) -> str:
        return f"DMWAS-{self.id:06d}" if self.id is not None else "Pending"


class PasswordResetCode(db.Model):
    """A short-lived, single-use email verification code for password recovery."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)


class MembershipDetails(db.Model):
    """Membership application details kept separate from account credentials."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    first_name = db.Column(db.String(60), nullable=False)
    last_name = db.Column(db.String(60), nullable=False)
    community = db.Column(db.String(120), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    age_19_or_older = db.Column(db.Boolean, nullable=False)
    mailing_address = db.Column(db.String(500), nullable=False)
    membership_type = db.Column(db.String(20), nullable=False)
    activities = db.Column(db.JSON, nullable=False, default=list)
    user = db.relationship("User", backref=db.backref("membership_details", uselist=False))


MEMBERSHIP_ACTIVITIES = (
    "Rock climbing",
    "Bouldering",
    "Hiking",
    "Mountaineering",
    "Trail development and maintenance",
    "Outdoor education",
)
VOLUNTEER_INTERESTS = (
    "Indoor climbing wall operations",
    "Outdoor trail maintenance",
    "Climbing route development",
    "Fundraising and community events",
    "Administrative or organizational work",
)
CONSENT_VERSIONS = {
    "membership_support": "membership-support-v2",
    "approval_understanding": "approval-understanding-v1",
    "privacy_acknowledgement": "privacy-acknowledgement-v1",
    "essential_communications": "essential-communications-v1",
    "newsletter": "newsletter-v1",
    "volunteer_contact": "volunteer-contact-v1",
    "guardian_consent": "guardian-consent-v1",
}
SOCIETY_ADMIN_EMAIL = "tharder@accessni.ca"


class MembershipPreferences(db.Model):
    """Volunteer preferences and contact details for an application."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    volunteer_interest = db.Column(db.Boolean, nullable=False)
    volunteer_interests = db.Column(db.JSON, nullable=False, default=list)
    volunteer_contact_ok = db.Column(db.Boolean, nullable=False, default=False)
    newsletter_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    guardian_name = db.Column(db.String(120), nullable=True)
    guardian_email = db.Column(db.String(254), nullable=True)
    user = db.relationship("User", backref=db.backref("membership_preferences", uselist=False))


class MembershipConsent(db.Model):
    """Versioned records of required acknowledgements and optional consent choices."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    consent_type = db.Column(db.String(50), nullable=False)
    version = db.Column(db.String(80), nullable=False)
    granted = db.Column(db.Boolean, nullable=False)
    consented_by = db.Column(db.String(120), nullable=True)
    contact_email = db.Column(db.String(254), nullable=True)
    accepted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    user = db.relationship("User", backref=db.backref("consent_records", lazy="select"))


class MembershipTerm(db.Model):
    """An immutable-ish record for each membership term, including renewals."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    term_number = db.Column(db.Integer, nullable=False)
    membership_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    approval_date = db.Column(db.Date, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    annual_dues_cents = db.Column(db.Integer, nullable=False)
    payment_status = db.Column(db.String(20), nullable=False)
    payment_date = db.Column(db.Date, nullable=True)
    payment_reference = db.Column(db.String(255), nullable=True)
    voting_eligible = db.Column(db.Boolean, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user = db.relationship("User", backref=db.backref("membership_terms", lazy="select"))
    __table_args__ = (db.UniqueConstraint("user_id", "term_number"),)


COMMUNITY_PROFILE_INTERESTS = MEMBERSHIP_ACTIVITIES
COMMUNITY_PROFILE_GOALS = (
    "Join Society activities",
    "Learn outdoor skills",
    "Volunteer with local projects",
    "Help maintain trails and climbing areas",
    "Connect with other adult members",
)


class CommunityProfile(db.Model):
    """Member-controlled community profile, separate from verified identity."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    display_name = db.Column(db.String(80), nullable=False)
    biography = db.Column(db.String(500), nullable=False, default="")
    general_community = db.Column(db.String(120), nullable=False, default="")
    interests = db.Column(db.JSON, nullable=False, default=list)
    participation_goals = db.Column(db.JSON, nullable=False, default=list)
    directory_opt_in = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    show_biography = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    show_community = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    show_interests = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    show_goals = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    photo = db.deferred(db.Column(db.LargeBinary, nullable=True))
    show_photo = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    show_highlights = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": version}
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    moderation_note = db.Column(db.String(500), nullable=True)
    moderated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    moderated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("community_profile", uselist=False),
    )
    moderator = db.relationship("User", foreign_keys=[moderated_by_id])
    reviews = db.relationship(
        "CommunityProfileReview",
        backref="profile",
        cascade="all, delete-orphan",
        order_by="CommunityProfileReview.reviewed_at",
    )


class CommunityProfileReview(db.Model):
    """Append-only-by-convention history of administrator profile decisions."""

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(
        db.Integer,
        db.ForeignKey("community_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    moderator_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    decision = db.Column(db.String(20), nullable=False)
    note = db.Column(db.String(500), nullable=True)
    reviewed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    moderator = db.relationship("User", foreign_keys=[moderator_user_id])


CommunityProfile.photo_present = db.column_property(CommunityProfile.photo.isnot(None))
db.Index("ix_community_profile_directory", CommunityProfile.status,
         CommunityProfile.directory_opt_in, func.lower(CommunityProfile.display_name), CommunityProfile.id)


class VerifiedCommunityRecord(db.Model):
    """Administrator-verified summary; separate from future activity rosters."""
    id = db.Column(db.Integer, primary_key=True)
    # Retained as a primary participant for compatibility with Phase 05 data.
    # Sharing eligibility is determined by participants, not this pointer.
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    record_type = db.Column(db.String(20), nullable=False)
    occurred_on = db.Column(db.Date, nullable=False)
    volunteer_minutes = db.Column(db.Integer, nullable=True)
    verified = db.Column(db.Boolean, nullable=False, default=True)
    shareable = db.Column(db.Boolean, nullable=False, default=False)
    sensitive = db.Column(db.Boolean, nullable=False, default=False)
    verified_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", foreign_keys=[user_id])
    participants = db.relationship("VerifiedCommunityRecordParticipant", cascade="all, delete-orphan",
                                   back_populates="record", order_by="VerifiedCommunityRecordParticipant.user_id")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if kwargs.get("user_id") is not None and "participants" not in kwargs:
            self.participants = [VerifiedCommunityRecordParticipant(user_id=kwargs["user_id"])]


class VerifiedCommunityRecordParticipant(db.Model):
    record_id = db.Column(db.Integer, db.ForeignKey("verified_community_record.id", ondelete="CASCADE"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True, index=True)
    record = db.relationship("VerifiedCommunityRecord", back_populates="participants")
    user = db.relationship("User")


class CommunityProfileHighlight(db.Model):
    profile_id = db.Column(db.Integer, db.ForeignKey("community_profile.id", ondelete="CASCADE"), primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey("verified_community_record.id", ondelete="CASCADE"), primary_key=True)
    record = db.relationship("VerifiedCommunityRecord")
    profile = db.relationship("CommunityProfile", backref=db.backref("highlights", cascade="all, delete-orphan"))


class VerifiedCommunityRecordAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey("verified_community_record.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    snapshot = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


def highlight_options(user_id):
    return VerifiedCommunityRecord.query.filter_by(verified=True,
        shareable=True, sensitive=False).filter(VerifiedCommunityRecord.participants.any(user_id=user_id)).order_by(VerifiedCommunityRecord.occurred_on.desc(),
                                                 VerifiedCommunityRecord.id).all()


def profile_query_options(query):
    return query.options(selectinload(CommunityProfile.highlights).selectinload(
        CommunityProfileHighlight.record).selectinload(VerifiedCommunityRecord.participants))


class DirectoryAccessWindow(db.Model):
    """Shared, bounded per-account discovery quota across web workers."""
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    window_number = db.Column(db.BigInteger, nullable=False)
    requests = db.Column(db.Integer, nullable=False)


def eligible_community_profiles():
    latest_term = select(func.max(MembershipTerm.term_number)).where(
        MembershipTerm.user_id == User.id
    ).correlate(User).scalar_subquery()
    return profile_query_options(CommunityProfile.query).join(User, CommunityProfile.user_id == User.id).join(
        MembershipDetails, MembershipDetails.user_id == User.id
    ).filter(
        CommunityProfile.directory_opt_in.is_(True), CommunityProfile.status == "approved",
        User.status == "active", MembershipDetails.date_of_birth <= adult_birth_date_cutoff(),
        or_(~User.membership_terms.any(), User.membership_terms.any(and_(
            MembershipTerm.term_number == latest_term,
            MembershipTerm.status == "active", MembershipTerm.expiry_date >= date.today(),
            MembershipTerm.payment_status.in_(("paid", "waived")),
        ))),
    )


def community_profile_fields(profile: CommunityProfile) -> dict[str, object]:
    """Allowlist fields for peers, APIs, previews and moderation; never identity."""
    fields: dict[str, object] = {"display_name": profile.display_name}
    for visible, key, value in (
        (profile.show_biography, "biography", profile.biography),
        (profile.show_community, "community", profile.general_community),
        (profile.show_interests, "interests", profile.interests),
        (profile.show_goals, "goals", profile.participation_goals),
    ):
        if visible:
            fields[key] = value
    if profile.show_photo and profile.photo_present:
        fields["photo_url"] = url_for("community_profile_photo", user_id=profile.user_id)
    if profile.show_highlights:
        records = [item.record for item in profile.highlights if any(
                   participant.user_id == profile.user_id for participant in item.record.participants)
                   and item.record.verified and item.record.shareable and not item.record.sensitive]
        fields["highlights"] = [{"title": record.title, "type": record.record_type,
                                  "date": record.occurred_on.isoformat(),
                                  **({"volunteer_minutes": record.volunteer_minutes}
                                     if record.record_type == "volunteer" else {})}
                                for record in sorted(records, key=lambda r: (r.occurred_on, r.id), reverse=True)]
    return fields


def delete_community_profile(user_id: int) -> bool:
    """Transactional erasure hook for self-service and account/retention workflows.

    The caller commits. No schedule is invented for records in backups.
    """
    profile = CommunityProfile.query.filter_by(user_id=user_id).with_for_update().first()
    if profile is None:
        return False
    db.session.delete(profile)
    db.session.flush()
    return True


def normalize_profile_photo(upload) -> bytes:
    """Decode a bounded JPEG/PNG and retain only resized, metadata-free pixels."""
    data = upload.stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Choose a photo smaller than 2 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=("JPEG", "PNG")) as image:
                if image.width * image.height > 16_000_000:
                    raise ValueError("Choose a photo with no more than 16 million pixels.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Choose a still JPEG or PNG photo.")
                image.load()
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((512, 512))
                clean = Image.new("RGB", oriented.size, "white")
                rgba = oriented.convert("RGBA")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = BytesIO()
                clean.save(output, format="JPEG", quality=85)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError("Choose a valid still JPEG or PNG photo.") from error


def _one_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def _expire_membership_if_due(user: User, today: date | None = None) -> bool:
    if user.status not in {"active", "inactive", "expired"}:
        return False
    latest_term = (
        MembershipTerm.query.filter_by(user_id=user.id)
        .order_by(MembershipTerm.term_number.desc())
        .first()
    )
    if not latest_term:
        if user.status == "expired":
            user.status = "inactive"
            return True
        return False

    current_date = today or date.today()
    changed = False
    expired_by_date = current_date > latest_term.expiry_date
    dues_unpaid = latest_term.payment_status not in {"paid", "waived"}
    if expired_by_date and latest_term.status == "active":
        latest_term.status = "expired"
        changed = True
    if (
        expired_by_date
        or dues_unpaid
        or latest_term.status != "active"
        or user.status == "expired"
    ) and user.status != "inactive":
        user.status = "inactive"
        changed = True
    return changed


def _is_19_or_older(birth_date: date, today: date | None = None) -> bool:
    today = today or date.today()
    age = today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )
    return age >= 19


def _is_voting_eligible(user: User, today: date | None = None) -> bool | None:
    """Return current eligibility; None means the applicant's age is unknown."""
    if not user.membership_details:
        return None
    return is_active_member(user, today) and _is_19_or_older(
        user.membership_details.date_of_birth, today
    )


def _database_uri() -> str:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///dmwas.db")
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return database_url


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be set to true or false.")


def _is_deployed_environment() -> bool:
    environment = os.environ.get("ENVIRONMENT", "").strip().lower()
    return bool(os.environ.get("RAILWAY_PROJECT_ID")) or environment in {
        "staging",
        "production",
    }


def _registration_policy(require_explicit: bool) -> RegistrationPolicy:
    defaults = RegistrationPolicy()
    options = {}
    for name, minimum, maximum in (
        ("offer_hours", 1, 168), ("signup_close_minutes", 0, 10080),
        ("cancellation_close_minutes", 0, 10080), ("renewal_grace_hours", 0, 168),
    ):
        env_name = "ACTIVITY_" + name.upper()
        value = os.environ.get(env_name)
        if value is None and not require_explicit:
            options[name] = getattr(defaults, name)
            continue
        if value is None or not re.fullmatch(r"[0-9]{1,5}", value) or not minimum <= int(value) <= maximum:
            raise RuntimeError(f"{env_name} must be set to a whole number from {minimum} to {maximum}.")
        options[name] = int(value)
    if require_explicit and "ACTIVITY_ALLOW_YOUTH_REGISTRATION" not in os.environ:
        raise RuntimeError("Set ACTIVITY_ALLOW_YOUTH_REGISTRATION explicitly before enabling registration.")
    options["allow_youth"] = _environment_flag("ACTIVITY_ALLOW_YOUTH_REGISTRATION", defaults.allow_youth)
    return RegistrationPolicy(**options)


def _messaging_policy(require_explicit: bool) -> MessagingPolicy:
    defaults = MessagingPolicy()
    options = {}
    for field, minimum, maximum in (
        ("digest_interval_hours", 1, 168), ("message_days", 1, 3650),
        ("closed_report_days", 1, 3650),
    ):
        setting = "MESSAGING_" + field.upper()
        value = os.environ.get(setting)
        if value is None and not require_explicit:
            options[field] = getattr(defaults, field)
            continue
        if value is None or not re.fullmatch(r"[0-9]{1,4}", value) or not minimum <= int(value) <= maximum:
            raise RuntimeError(f"{setting} must be a whole number from {minimum} through {maximum}.")
        options[field] = int(value)
    raw = os.environ.get("MESSAGING_MODERATOR_IDS", "").strip()
    ids = [value.strip() for value in raw.split(",")] if raw else []
    if (require_explicit and not ids) or any(
        not re.fullmatch(r"[0-9]{1,10}", value) or not 0 < int(value) <= 2147483647 for value in ids
    ):
        raise RuntimeError("MESSAGING_MODERATOR_IDS must list designated administrator account IDs.")
    options["moderator_ids"] = frozenset(map(int, ids))
    return MessagingPolicy(**options)


def _password_reset_code_hash(secret_key: str, email: str, code: str) -> str:
    """Hash a low-entropy code with the app secret so database leaks don't expose it."""
    message = f"{email.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _send_resend_email(
    recipient: str,
    subject: str,
    text: str,
    reply_to: str | None = None,
    from_address: str | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Send a plain-text email through Resend's HTTPS API."""
    api_key = os.environ.get("RESEND_API_KEY")
    sender = from_address or os.environ.get("RESEND_FROM") or os.environ.get("SMTP_FROM")
    if not api_key or not sender:
        logging.getLogger(__name__).info(
            "Resend is not configured; skipped email delivery."
        )
        return False

    email_payload = {"from": sender, "to": [recipient], "subject": subject, "text": text}
    if reply_to:
        email_payload["reply_to"] = reply_to
    payload = json.dumps(
        email_payload
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "DMWAS-website/1.0",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    resend_request = urllib_request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib_request.urlopen(resend_request, timeout=10) as response:
        response.read()
    return True


def _send_password_reset_code(user: User, code: str) -> None:
    """Send the password-reset verification code through Resend."""
    _send_resend_email(
        user.email,
        "Your DMWAS password reset code",
        (
            f"Hello {user.full_name},\n\n"
            f"Your password reset code is: {code}\n\n"
            "This code expires in 10 minutes and can only be used once. "
            "If you did not request a password reset, you can ignore this email."
        ),
    )


def _send_request_notification(user: User) -> None:
    """Send an optional membership request notification through Resend."""
    _send_resend_email(
        os.environ.get("ADMIN_NOTIFICATION_EMAIL", "members@accessni.ca"),
        "New DMWAS membership access request",
        (
            "A membership access request is waiting for review.\n\n"
            f"Name: {user.full_name}\n"
            f"Email: {user.email}\n"
            f"Username: {user.username}\n\n"
            "Sign in to the Society website's admin area to review it."
        ),
    )


def _send_renewal_request_notification(user: User) -> None:
    """Notify administrators that an inactive member requested renewal."""
    _send_resend_email(
        os.environ.get("ADMIN_NOTIFICATION_EMAIL", "members@accessni.ca"),
        "DMWAS membership renewal awaiting review",
        (
            "A member has submitted a renewal request. Review the member's "
            "payment and renewal in the administrator member list.\n\n"
            f"Member: {user.member_number}\n"
            f"Name: {user.full_name}\n"
            f"Email: {user.email}"
        ),
    )


def _send_membership_application_confirmation(user: User) -> None:
    """Thank an applicant and explain their pending account and dues status."""
    details = user.membership_details
    if details and details.membership_type == "adult":
        membership_status = (
            "Adult membership has annual dues of $10. Please send payment by "
            "Interac e-Transfer to membership@accessni.ca. Your account will "
            "remain pending until the Society receives payment and approves "
            "your application."
        )
    else:
        membership_status = (
            "Youth membership is free, so no payment is required. Your account "
            "will remain pending until the Society reviews and approves your "
            "application."
        )

    _send_resend_email(
        user.email,
        "Thank you for your DMWAS membership application",
        (
            f"Hello {user.full_name},\n\n"
            "Thank you for creating an account and submitting a membership "
            "application with the District of Mount Waddington Access Society.\n\n"
            f"{membership_status}\n\n"
            "We will notify you when your application has been reviewed."
        ),
    )


def _send_membership_approval_notification(user: User) -> None:
    """Notify a member that their account has been approved."""
    _send_resend_email(
        user.email,
        "Congratulations—your DMWAS membership has been approved",
        (
            f"Hello {user.full_name},\n\n"
            "Congratulations your membership has been approved. Your account "
            "is now active, and you can sign in to access the member area."
        ),
    )


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    app.config.from_mapping(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=_database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"hide_parameters": True},
        PUBLIC_CONTACT_EMAIL=os.environ.get("PUBLIC_CONTACT_EMAIL", ""),
        PUBLIC_CONTACT_PHONE=os.environ.get("PUBLIC_CONTACT_PHONE", ""),
        PUBLIC_CONTACT_ADDRESS=os.environ.get("PUBLIC_CONTACT_ADDRESS", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_environment_flag("SESSION_COOKIE_SECURE", False),
        MEMBER_ACTIVITY_PUBLICATION_ENABLED=_environment_flag(
            "MEMBER_ACTIVITY_PUBLICATION_ENABLED", False
        ),
        ACTIVITY_REGISTRATION_ENABLED=_environment_flag(
            "ACTIVITY_REGISTRATION_ENABLED", not _is_deployed_environment()
        ),
        ACTIVITY_ACTIONS_PER_MINUTE=30,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        DIRECTORY_REQUESTS_PER_MINUTE=60,
        COMMUNITY_MESSAGING_ENABLED=_environment_flag(
            "COMMUNITY_MESSAGING_ENABLED", not _is_deployed_environment()
        ),
        MESSAGING_REQUESTS_PER_DAY=5,
        MESSAGING_REPORTS_PER_DAY=5,
        MESSAGING_MESSAGES_PER_MINUTE=30,
    )
    if test_config:
        app.config.update(test_config)

    if _is_deployed_environment() and not app.config.get("TESTING"):
        if not secret_key:
            raise RuntimeError("SECRET_KEY must be configured in staging and production.")
        if len(str(app.config["SECRET_KEY"])) < 32:
            raise RuntimeError("SECRET_KEY must contain at least 32 characters in deployed environments.")
        database_uri = str(app.config["SQLALCHEMY_DATABASE_URI"])
        if not database_uri.startswith(("postgresql+psycopg2://", "postgresql://", "postgres://")):
            raise RuntimeError("DATABASE_URL must point to PostgreSQL in deployed environments.")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            raise RuntimeError("SESSION_COOKIE_SECURE=true is required in staging and production.")

    if "ACTIVITY_REGISTRATION_POLICY" not in app.config:
        app.config["ACTIVITY_REGISTRATION_POLICY"] = _registration_policy(
            _is_deployed_environment() and not app.config["TESTING"]
            and app.config["ACTIVITY_REGISTRATION_ENABLED"]
        )

    if "MESSAGING_POLICY" not in app.config:
        app.config["MESSAGING_POLICY"] = (
            _messaging_policy(_is_deployed_environment() and not app.config["TESTING"])
            if app.config["COMMUNITY_MESSAGING_ENABLED"] else MessagingPolicy()
        )

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)

    @app.before_request
    def protect_post_requests() -> None:
        if request.method != "POST":
            return
        if request.blueprint == "messaging":
            request.max_content_length = 24 * 1024
        expected_token = session.get("csrf_token")
        submitted_token = request.form.get("csrf_token", "")
        if (not expected_token or not submitted_token.isascii()
                or not secrets.compare_digest(expected_token, submitted_token)):
            abort(400, description="The form expired or was invalid. Please try again.")

    @app.before_request
    def load_current_user() -> None:
        user_id = session.get("user_id")
        g.current_user = db.session.get(User, user_id) if user_id else None
        user = g.current_user
        if user and _expire_membership_if_due(user):
            db.session.commit()

    @app.before_request
    def limit_directory_access():
        if request.endpoint not in {"member_search", "community_directory_api",
                                    "community_profile_api", "community_profile_view",
                                    "community_profile_photo"}:
            return
        if not has_capability(g.current_user, Capability.ADULT_DIRECTORY):
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if db.engine.dialect.name == "postgresql" else sqlite_insert
        window = int(datetime.now(timezone.utc).timestamp()) // 60
        quota = int(app.config["DIRECTORY_REQUESTS_PER_MINUTE"])
        statement = insert(DirectoryAccessWindow).values(
            user_id=g.current_user.id, window_number=window, requests=1)
        statement = statement.on_conflict_do_update(
            index_elements=[DirectoryAccessWindow.user_id],
            set_={"window_number": window, "requests": case(
                (DirectoryAccessWindow.window_number == window, DirectoryAccessWindow.requests + 1),
                else_=1)},
            where=or_(DirectoryAccessWindow.window_number != window,
                      DirectoryAccessWindow.requests < quota),
        ).returning(DirectoryAccessWindow.requests)
        allowed = db.session.execute(statement).scalar_one_or_none()
        db.session.commit()
        if allowed is None:
            retry_after = 60 - int(datetime.now(timezone.utc).timestamp()) % 60
            message = "Too many directory requests. Please try again shortly."
            body = {"error": message} if request.path.startswith("/api/") else render_template(
                "directory_rate_limit.html", title="Please wait", retry_after=retry_after)
            return body, 429, {"Retry-After": str(retry_after)}

    @app.context_processor
    def inject_template_values() -> dict[str, object]:
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        current_user = g.get("current_user")
        can_review_members = has_capability(current_user, Capability.ADMIN_REVIEW)
        return {
            "current_user": current_user,
            "can_access_member_area": has_capability(current_user, Capability.ACCOUNT_ACCESS),
            "can_manage_profile": has_capability(current_user, Capability.PROFILE_MANAGEMENT),
            "can_review_members": can_review_members,
            "can_read_messages": app.config["COMMUNITY_MESSAGING_ENABLED"] and history_access(current_user),
            "can_request_renewal": has_capability(current_user, Capability.RENEWAL_REQUEST),
            "can_review_message_reports": (
                app.config["COMMUNITY_MESSAGING_ENABLED"]
                and is_moderator(current_user, app.config["MESSAGING_POLICY"])
            ),
            "can_use_activity_registration": has_capability(
                current_user, Capability.ACTIVITY_REGISTRATION
            ),
            "can_use_activity_workspace": (
                has_capability(current_user, Capability.ACTIVITY_PROPOSAL)
                or can_review_members
            ),
            "can_access_adult_hub": has_capability(
                current_user, Capability.ADULT_DIRECTORY
            ),
        }

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if (request.blueprint in {"activities", "registrations", "messaging"}
                or request.path.startswith(("/member", "/api/community", "/admin/community"))):
            response.headers["Cache-Control"] = "private, no-store"
            response.vary.add("Cookie")
        return response

    def login_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            user = g.get("current_user")
            if not has_capability(user, Capability.ACCOUNT_ACCESS):
                session.clear()
                flash("Please sign in with an approved member account.", "info")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return cast(F, wrapped)

    def adult_member_required(view: F) -> F:
        @wraps(view)
        @login_required
        def wrapped(*args: object, **kwargs: object):
            if not has_capability(g.get("current_user"), Capability.ADULT_DIRECTORY):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    def profile_owner_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            if not has_capability(g.get("current_user"), Capability.PROFILE_MANAGEMENT):
                if request.path.startswith("/api/"):
                    abort(403)
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return cast(F, wrapped)

    def renewal_access_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            user = g.get("current_user")
            if has_capability(user, Capability.ACCOUNT_ACCESS):
                session.pop("renewal_only", None)
                return redirect(url_for("member_home"))
            if not session.get("renewal_only") or not has_capability(
                user, Capability.RENEWAL_REQUEST
            ):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    def admin_required(view: F) -> F:
        @wraps(view)
        @login_required
        def wrapped(*args: object, **kwargs: object):
            if not has_capability(g.current_user, Capability.ADMIN_REVIEW):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    @app.get("/")
    def index():
        return redirect(url_for("about"))

    @app.get("/about")
    def about():
        return render_template("about.html", title="About")

    @app.get("/notices")
    def notices():
        return render_template("notices.html", title="Notices")

    @app.get("/contact")
    @app.post("/contact")
    def contact():
        contact_values = {
            "name": "",
            "email": "",
            "subject": "",
            "message": "",
        }
        if request.method == "POST":
            # Quietly discard common bot submissions to the hidden honeypot field.
            if request.form.get("website", "").strip():
                flash("Your message has been sent. Thank you for contacting the Society.", "success")
                return redirect(url_for("contact"))

            contact_values = {
                "name": request.form.get("name", "").strip(),
                "email": request.form.get("email", "").strip().lower(),
                "subject": request.form.get("subject", "").strip(),
                "message": request.form.get("message", "").strip(),
            }
            errors = []

            if not contact_values["name"] or len(contact_values["name"]) > 120:
                errors.append("Enter your name (up to 120 characters).")
            if not EMAIL_PATTERN.fullmatch(contact_values["email"]):
                errors.append("Enter a valid email address.")
            if not contact_values["subject"] or len(contact_values["subject"]) > 160:
                errors.append("Enter a subject (up to 160 characters).")
            if not contact_values["message"] or len(contact_values["message"]) > 5000:
                errors.append("Enter a message (up to 5,000 characters).")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "contact.html", title="Contact", contact_values=contact_values
                ), 400

            email_text = (
                f"Name: {contact_values['name']}\n"
                f"Email: {contact_values['email']}\n\n"
                f"{contact_values['message']}"
            )
            try:
                sent = _send_resend_email(
                    "inquiries@accessni.ca",
                    f"Contact form: {contact_values['subject']}",
                    email_text,
                    reply_to=contact_values["email"],
                    from_address="DMWAS <inquiries@accessni.ca>",
                )
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                request_id = error.headers.get("x-resend-id", "unknown")
                app.logger.error(
                    "Resend rejected contact form email: HTTP %s, request id %s, response %s",
                    error.code,
                    request_id,
                    detail,
                )
                sent = False
            except (OSError, ValueError):
                app.logger.exception("Could not send contact form email")
                sent = False

            if sent:
                flash("Your message has been sent. Thank you for contacting the Society.", "success")
                return redirect(url_for("contact"))

            flash(
                "We could not send your message right now. Please try again later or use the contact details listed above.",
                "error",
            )

        return render_template(
            "contact.html", title="Contact", contact_values=contact_values
        )

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", title="Privacy information")

    @app.get("/donate")
    def donate():
        return render_template("donate.html", title="Donate")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and _expire_membership_if_due(user):
                db.session.commit()
            if user and user.check_password(password):
                destination = request.args.get("next", "")
                if has_capability(user, Capability.ACCOUNT_ACCESS):
                    session.clear()
                    session["user_id"] = user.id
                    flash("You are signed in.", "success")
                    if destination.startswith("/") and not destination.startswith("//"):
                        return redirect(destination)
                    return redirect(url_for("member_home"))
                can_renew = has_capability(user, Capability.RENEWAL_REQUEST)
                has_history = (
                    app.config["COMMUNITY_MESSAGING_ENABLED"] and history_access(user)
                    and MessagePair.query.filter(participating(user.id), MessagePair.accepted_at.is_not(None)).first()
                )
                if can_renew or has_history:
                    session.clear()
                    session["user_id"] = user.id
                    session["renewal_only"] = True
                    if has_history and re.fullmatch(r"/member/messages(?:/[0-9]+(?:/report)?)?", destination):
                        return redirect(destination)
                    if can_renew:
                        flash("Your membership is inactive. Submit a renewal request to restore access.",
                              "info")
                        return redirect(url_for("membership_renewal"))
                    return redirect(url_for("messaging.inbox"))
            flash("The username or password was not accepted, or the account is awaiting approval.", "error")
        return render_template("login.html", title="Member sign in")

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            if not EMAIL_PATTERN.fullmatch(email):
                flash("Enter a valid email address.", "error")
                return render_template("forgot_password.html", title="Forgot password"), 400

            session["password_reset_email"] = email
            user = User.query.filter_by(email=email).first()
            if user:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                latest_code = (
                    PasswordResetCode.query.filter_by(user_id=user.id)
                    .order_by(PasswordResetCode.created_at.desc())
                    .first()
                )
                # Limit email abuse while allowing a fresh request after one minute.
                if not latest_code or now - latest_code.created_at >= timedelta(minutes=1):
                    PasswordResetCode.query.filter_by(
                        user_id=user.id, used_at=None
                    ).update({"used_at": now}, synchronize_session=False)
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    reset_code = PasswordResetCode(
                        user_id=user.id,
                        code_hash=_password_reset_code_hash(
                            str(app.config["SECRET_KEY"]), email, code
                        ),
                        created_at=now,
                        expires_at=now + timedelta(minutes=10),
                    )
                    db.session.add(reset_code)
                    db.session.commit()
                    try:
                        _send_password_reset_code(user, code)
                    except HTTPError as error:
                        detail = error.read().decode("utf-8", errors="replace")
                        request_id = error.headers.get("x-resend-id", "unknown")
                        app.logger.error(
                            "Resend rejected password reset email: HTTP %s, "
                            "request id %s, response %s",
                            error.code,
                            request_id,
                            detail,
                        )
                    except (OSError, ValueError):
                        app.logger.exception("Could not send password reset code")

            flash(
                "If an account exists for that email, a verification code will be sent. "
                "Check your inbox and spam folder.",
                "success",
            )
            return redirect(url_for("reset_password"))

        return render_template("forgot_password.html", title="Forgot password")

    @app.route("/reset-password", methods=["GET", "POST"])
    def reset_password():
        email = session.get("password_reset_email", "")
        if not email:
            flash("Request a password reset code first.", "info")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            code = request.form.get("code", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            user = User.query.filter_by(email=email).first()
            reset_code = None
            if user:
                reset_code = (
                    PasswordResetCode.query.filter_by(user_id=user.id, used_at=None)
                    .order_by(PasswordResetCode.created_at.desc())
                    .first()
                )

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if not reset_code or reset_code.expires_at <= now:
                if reset_code:
                    reset_code.used_at = now
                    db.session.commit()
                flash("That code is invalid or expired. Request a new code and try again.", "error")
                return render_template("reset_password.html", title="Reset password"), 400

            if reset_code.attempts >= 5:
                reset_code.used_at = now
                db.session.commit()
                flash("Too many incorrect attempts. Request a new code.", "error")
                return render_template("reset_password.html", title="Reset password"), 400

            expected_hash = _password_reset_code_hash(
                str(app.config["SECRET_KEY"]), email, code
            )
            if not re.fullmatch(r"\d{6}", code) or not hmac.compare_digest(
                reset_code.code_hash, expected_hash
            ):
                reset_code.attempts += 1
                if reset_code.attempts >= 5:
                    reset_code.used_at = now
                db.session.commit()
                flash("That code is invalid or expired. Check it and try again.", "error")
                return render_template("reset_password.html", title="Reset password"), 400

            if len(password) < MIN_PASSWORD_LENGTH:
                flash(
                    f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                    "error",
                )
                return render_template("reset_password.html", title="Reset password"), 400
            if password != confirm_password:
                flash("The passwords do not match.", "error")
                return render_template("reset_password.html", title="Reset password"), 400

            user.set_password(password)
            reset_code.used_at = now
            # Also invalidate any older outstanding code for this account.
            PasswordResetCode.query.filter(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.id != reset_code.id,
                PasswordResetCode.used_at.is_(None),
            ).update({"used_at": now}, synchronize_session=False)
            db.session.commit()
            session.pop("password_reset_email", None)
            flash("Your password has been reset. Sign in with your new password.", "success")
            return redirect(url_for("login"))

        return render_template("reset_password.html", title="Reset password")

    @app.route("/membership/request", methods=["GET", "POST"])
    def membership_request():
        if request.method == "POST":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            community = request.form.get("community", "").strip()
            date_of_birth_value = request.form.get("date_of_birth", "").strip()
            mailing_address = request.form.get("mailing_address", "").strip()
            membership_type = request.form.get("membership_type", "")
            activities = request.form.getlist("activities")
            volunteer_choices = request.form.getlist("volunteer_interests")
            volunteer_interest = any(
                choice in VOLUNTEER_INTERESTS for choice in volunteer_choices
            )
            volunteer_contact_value = request.form.get("volunteer_contact_ok", "")
            newsletter_opt_in = request.form.get("newsletter_opt_in") == "yes"
            acknowledgements = {
                "membership_support": request.form.get("agree_membership_support") == "yes",
                "approval_understanding": request.form.get("acknowledge_approval") == "yes",
                "privacy_acknowledgement": request.form.get("acknowledge_privacy") == "yes",
                "essential_communications": request.form.get("essential_communications") == "yes",
            }
            guardian_name = request.form.get("guardian_name", "").strip()
            guardian_email = request.form.get("guardian_email", "").strip().lower()
            guardian_consent = request.form.get("guardian_consent") == "yes"
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            errors: list[str] = []
            if not first_name or len(first_name) > 60:
                errors.append("Enter your first name (up to 60 characters).")
            if not last_name or len(last_name) > 60:
                errors.append("Enter your last name (up to 60 characters).")
            if not EMAIL_PATTERN.fullmatch(email):
                errors.append("Enter a valid email address.")
            if not phone or len(phone) > 40:
                errors.append("Enter a phone number of 40 characters or fewer.")
            if not community or len(community) > 120:
                errors.append("Enter your community of residence (up to 120 characters).")
            try:
                date_of_birth = date.fromisoformat(date_of_birth_value)
                if date_of_birth > date.today():
                    raise ValueError
            except ValueError:
                date_of_birth = None
                errors.append("Enter a valid date of birth that is not in the future.")
            if not mailing_address or len(mailing_address) > 500:
                errors.append("Enter your mailing address (up to 500 characters).")
            if membership_type not in {"adult", "youth"}:
                errors.append("Select an adult or youth membership.")
            if any(activity not in MEMBERSHIP_ACTIVITIES for activity in activities):
                errors.append("Select only activities from the list.")
            if any(
                choice != "not_at_this_time" and choice not in VOLUNTEER_INTERESTS
                for choice in volunteer_choices
            ):
                errors.append("Select only volunteer interests from the list.")
            if "not_at_this_time" in volunteer_choices and len(volunteer_choices) > 1:
                errors.append("Select “Not at this time” by itself, or choose volunteer activities.")
            if volunteer_contact_value not in {"yes", "no"}:
                errors.append("Choose whether you would like to be contacted about volunteer opportunities.")
            if not all(acknowledgements.values()):
                errors.append("Accept all required membership and communication acknowledgements to apply.")
            if date_of_birth:
                is_adult = _is_19_or_older(date_of_birth)
                expected_membership = "adult" if is_adult else "youth"
                if membership_type != expected_membership:
                    errors.append("Your membership type must match your age: adults are 19 or older; youth are under 19.")
                if not is_adult:
                    if not guardian_name or len(guardian_name) > 120:
                        errors.append("A parent or guardian must provide their name for a youth application.")
                    if not EMAIL_PATTERN.fullmatch(guardian_email):
                        errors.append("A parent or guardian must provide a valid email address for a youth application.")
                    if not guardian_consent:
                        errors.append("A parent or guardian must confirm consent for a youth application.")
            if not re.fullmatch(r"[a-z0-9_.-]{3,40}", username):
                errors.append("Choose a username of 3–40 letters, numbers, dots, underscores, or hyphens.")
            if len(password) < MIN_PASSWORD_LENGTH:
                errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if password != confirm_password:
                errors.append("The passwords do not match.")
            if User.query.filter_by(email=email).first() or User.query.filter_by(username=username).first():
                errors.append("That email or username cannot be used. Contact the Society if you already requested access.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("membership_request.html", title="Membership application"), 400

            assert date_of_birth is not None
            full_name = f"{first_name} {last_name}"
            user = User(
                full_name=full_name,
                email=email,
                phone=phone,
                username=username,
                status="pending",
            )
            user.set_password(password)
            user.membership_details = MembershipDetails(
                first_name=first_name,
                last_name=last_name,
                community=community,
                date_of_birth=date_of_birth,
                age_19_or_older=_is_19_or_older(date_of_birth),
                mailing_address=mailing_address,
                membership_type=membership_type,
                activities=activities,
            )
            user.membership_preferences = MembershipPreferences(
                volunteer_interest=volunteer_interest,
                volunteer_interests=[
                    choice for choice in volunteer_choices if choice in VOLUNTEER_INTERESTS
                ],
                volunteer_contact_ok=volunteer_contact_value == "yes",
                newsletter_opt_in=newsletter_opt_in,
                guardian_name=guardian_name or None,
                guardian_email=guardian_email or None,
            )
            consent_grants = {
                **acknowledgements,
                "newsletter": newsletter_opt_in,
                "volunteer_contact": volunteer_contact_value == "yes",
            }
            if not _is_19_or_older(date_of_birth):
                consent_grants["guardian_consent"] = guardian_consent
            for consent_type, granted in consent_grants.items():
                user.consent_records.append(
                    MembershipConsent(
                        consent_type=consent_type,
                        version=CONSENT_VERSIONS[consent_type],
                        granted=granted,
                        consented_by=guardian_name if consent_type == "guardian_consent" else None,
                        contact_email=guardian_email if consent_type == "guardian_consent" else None,
                        accepted_at=datetime.now(timezone.utc),
                    )
                )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That email or username cannot be used. Contact the Society if you already requested access.", "error")
                return render_template("membership_request.html", title="Membership application"), 400

            try:
                _send_request_notification(user)
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                request_id = error.headers.get("x-resend-id", "unknown")
                app.logger.error(
                    "Resend rejected membership request notification: HTTP %s, "
                    "request id %s, response %s",
                    error.code,
                    request_id,
                    detail,
                )
            except (OSError, ValueError):
                app.logger.exception("Could not send membership request notification")
            try:
                _send_membership_application_confirmation(user)
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                request_id = error.headers.get("x-resend-id", "unknown")
                app.logger.error(
                    "Resend rejected membership application confirmation: HTTP %s, "
                    "request id %s, response %s",
                    error.code,
                    request_id,
                    detail,
                )
            except (OSError, ValueError):
                app.logger.exception(
                    "Could not send membership application confirmation"
                )
            flash("Your membership application has been received. Sign-in will be available after Society approval.", "success")
            return redirect(url_for("login"))

        return render_template("membership_request.html", title="Membership application")

    @app.route("/membership/renew", methods=["GET", "POST"])
    @renewal_access_required
    def membership_renewal():
        user = g.current_user
        latest_term = (
            MembershipTerm.query.filter_by(user_id=user.id)
            .order_by(MembershipTerm.term_number.desc())
            .first()
        )
        if not latest_term:
            abort(404)

        if request.method == "POST":
            if latest_term.status == "pending":
                flash("Your renewal request is already awaiting administrator review.", "info")
                return redirect(url_for("membership_renewal"))

            today = date.today()
            membership_type = latest_term.membership_type
            annual_dues_cents = 1000 if membership_type == "adult" else 0
            user.membership_terms.append(
                MembershipTerm(
                    term_number=latest_term.term_number + 1,
                    membership_type=membership_type,
                    status="pending",
                    # These dates are provisional until an administrator approves.
                    approval_date=today,
                    start_date=today,
                    expiry_date=today,
                    annual_dues_cents=annual_dues_cents,
                    payment_status="unpaid" if annual_dues_cents else "waived",
                    voting_eligible=(
                        _is_19_or_older(user.membership_details.date_of_birth, today)
                        if user.membership_details
                        else None
                    ),
                )
            )
            db.session.commit()
            try:
                _send_renewal_request_notification(user)
            except HTTPError as error:
                app.logger.error(
                    "Resend rejected membership renewal notification: HTTP %s, request id %s",
                    error.code,
                    error.headers.get("x-resend-id", "unknown"),
                )
            except (OSError, ValueError):
                app.logger.exception("Could not send membership renewal notification")
            flash(
                "Your renewal request was submitted. Member access will remain "
                "inactive until any dues are recorded and an administrator approves it.",
                "success",
            )
            return redirect(url_for("membership_renewal"))

        return render_template(
            "membership_renewal.html",
            title="Renew membership",
            latest_term=latest_term,
            renewal_pending=latest_term.status == "pending",
        )

    @app.post("/logout")
    def logout():
        user = g.get("current_user")
        member_access = has_capability(user, Capability.ACCOUNT_ACCESS)
        renewal_access = session.get("renewal_only") and has_capability(
            user, Capability.RENEWAL_REQUEST
        )
        if not member_access and not renewal_access:
            abort(403)
        session.clear()
        flash("You are signed out.", "success")
        return redirect(url_for("about"))

    @app.get("/member")
    @login_required
    def member_home():
        return render_template("member_home.html", title="Member area")

    @app.get("/member/profile")
    @profile_owner_required
    def manage_profile():
        return render_template("manage_profile.html", title="Manage Profile")

    @app.route("/member/profile/account", methods=["GET", "POST"])
    @profile_owner_required
    def member_account_edit():
        user = g.current_user
        if request.method == "POST":
            user = User.query.populate_existing().with_for_update().filter_by(id=user.id).first_or_404()
        details = user.membership_details
        preferences = user.membership_preferences
        show_guardian = bool(
            (details and details.membership_type == "youth")
            or (preferences and (preferences.guardian_name or preferences.guardian_email))
        )
        errors = []

        if request.method == "POST":
            phone = request.form.get("phone", "").strip()
            if len(phone) > 40:
                errors.append("Enter a phone number of up to 40 characters.")

            if details:
                first_name = request.form.get("first_name", "").strip()
                last_name = request.form.get("last_name", "").strip()
                full_name = f"{first_name} {last_name}"
                if (not first_name or not last_name
                        or max(len(first_name), len(last_name)) > 60 or len(full_name) > 120):
                    errors.append("Enter your first and last names (up to 60 characters each and 120 combined).")
                community = request.form.get("community", "").strip()
                mailing_address = request.form.get("mailing_address", "").strip()
                activities = list(dict.fromkeys(request.form.getlist("activities")))
                if not community or len(community) > 120:
                    errors.append("Enter your community of residence (up to 120 characters).")
                if not mailing_address or len(mailing_address) > 500:
                    errors.append("Enter your mailing address (up to 500 characters).")
                if any(activity not in MEMBERSHIP_ACTIVITIES for activity in activities):
                    errors.append("Select only activities from the list.")
            else:
                full_name = request.form.get("full_name", "").strip()
                if not full_name or len(full_name) > 120:
                    errors.append("Enter your full name (up to 120 characters).")

            volunteer_choices = list(dict.fromkeys(request.form.getlist("volunteer_interests")))
            if any(choice not in VOLUNTEER_INTERESTS for choice in volunteer_choices):
                errors.append("Select only volunteer interests from the list.")
            if show_guardian:
                guardian_name = request.form.get("guardian_name", "").strip()
                guardian_email = request.form.get("guardian_email", "").strip().lower()
                if guardian_name or guardian_email:
                    if not guardian_name or len(guardian_name) > 120:
                        errors.append("Enter your parent or guardian's name (up to 120 characters).")
                    if len(guardian_email) > 254 or not EMAIL_PATTERN.fullmatch(guardian_email):
                        errors.append("Enter a valid parent or guardian email address.")
                elif preferences and (preferences.guardian_name or preferences.guardian_email):
                    errors.append("Enter your parent or guardian's contact details.")

            if not errors:
                user.full_name = full_name
                user.phone = phone or None

                if details:
                    details.first_name = first_name
                    details.last_name = last_name
                    details.community = community
                    details.mailing_address = mailing_address
                    details.activities = activities
                if preferences is None:
                    preferences = user.membership_preferences = MembershipPreferences()
                preferences.volunteer_interest = bool(volunteer_choices)
                preferences.volunteer_interests = volunteer_choices
                for consent_type, attribute in (
                    ("newsletter", "newsletter_opt_in"),
                    ("volunteer_contact", "volunteer_contact_ok"),
                ):
                    granted = request.form.get(attribute) == "yes"
                    if granted != bool(getattr(preferences, attribute)):
                        db.session.add(MembershipConsent(
                            user_id=user.id,
                            consent_type=consent_type,
                            version=CONSENT_VERSIONS[consent_type],
                            granted=granted,
                        ))
                    setattr(preferences, attribute, granted)
                if show_guardian:
                    preferences.guardian_name = guardian_name or None
                    preferences.guardian_email = guardian_email or None

                db.session.commit()
                flash("Your account information was saved.", "success")
                return redirect(url_for("manage_profile"))

        for error in errors:
            flash(error, "error")
        return render_template(
            "member_account_edit.html",
            title="Edit account information",
            details=user.membership_details,
            preferences=user.membership_preferences,
            activities=MEMBERSHIP_ACTIVITIES,
            volunteer_interests=VOLUNTEER_INTERESTS,
            show_guardian=show_guardian,
        ), 400 if errors else 200

    @app.post("/member/profile")
    @app.route("/member/profile/community/edit", methods=["GET", "POST"])
    @profile_owner_required
    def member_profile():
        user = g.current_user
        profile = profile_query_options(CommunityProfile.query).filter_by(user_id=user.id).with_for_update().first()
        highlight_records = highlight_options(user.id)

        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            biography = request.form.get("biography", "").strip()
            general_community = request.form.get("general_community", "").strip()
            requested_interests = list(dict.fromkeys(request.form.getlist("interests")))
            requested_goals = list(dict.fromkeys(request.form.getlist("participation_goals")))
            directory_opt_in = request.form.get("directory_opt_in") == "yes"
            errors = []

            try:
                chosen_highlights = set(int(value) for value in request.form.getlist("highlights"))
            except ValueError:
                chosen_highlights = set()
                errors.append("Choose valid verified highlights.")
            allowed_highlights = {record.id for record in highlight_records}
            if len(chosen_highlights) > 10 or not chosen_highlights <= allowed_highlights:
                errors.append("Choose up to ten of your own verified, shareable highlights.")

            if not display_name or len(display_name) > 80:
                errors.append("Choose a display name of 1–80 characters.")
            if len(biography) > 500:
                errors.append("Biography must be 500 characters or fewer.")
            if len(general_community) > 120:
                errors.append("Community must be 120 characters or fewer.")
            if any(value not in COMMUNITY_PROFILE_INTERESTS for value in requested_interests):
                errors.append("Choose interests from the provided list.")
            if any(value not in COMMUNITY_PROFILE_GOALS for value in requested_goals):
                errors.append("Choose participation goals from the provided list.")
            if directory_opt_in and not has_capability(user, Capability.ADULT_DIRECTORY):
                errors.append("Only active adult members may opt in to the directory.")
            photo_upload = request.files.get("photo")
            photo_data = None
            if photo_upload and photo_upload.filename:
                try:
                    photo_data = normalize_profile_photo(photo_upload)
                except ValueError as error:
                    errors.append(str(error))

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "member_profile_edit.html",
                    title="Edit community profile",
                    profile=profile,
                    interests=COMMUNITY_PROFILE_INTERESTS,
                    goals=COMMUNITY_PROFILE_GOALS,
                    highlight_records=highlight_records,
                ), 400

            if profile is None:
                profile = CommunityProfile(user_id=user.id, display_name=display_name)
                db.session.add(profile)
            profile.display_name = display_name
            profile.biography = biography
            profile.general_community = general_community
            profile.interests = requested_interests
            profile.participation_goals = requested_goals
            profile.directory_opt_in = directory_opt_in
            profile.show_biography = request.form.get("show_biography") == "yes"
            profile.show_community = request.form.get("show_community") == "yes"
            profile.show_interests = request.form.get("show_interests") == "yes"
            profile.show_goals = request.form.get("show_goals") == "yes"
            profile.show_photo = request.form.get("show_photo") == "yes"
            profile.show_highlights = request.form.get("show_highlights") == "yes"
            for selected in list(profile.highlights):
                if selected.record_id not in chosen_highlights:
                    profile.highlights.remove(selected)
            existing_highlights = {item.record_id for item in profile.highlights}
            for record_id in chosen_highlights - existing_highlights:
                profile.highlights.append(CommunityProfileHighlight(record_id=record_id))
            if request.form.get("remove_photo") == "yes":
                profile.photo = None
            elif photo_data is not None:
                profile.photo = photo_data
            # Every eligible adult member's sharing submission is approved on save.
            auto_approve = (
                directory_opt_in
                and has_capability(user, Capability.ADULT_DIRECTORY)
            )
            profile.status = "pending" if directory_opt_in else "draft"
            profile.moderation_note = None
            profile.moderated_by_id = None
            profile.moderated_at = None
            if auto_approve:
                profile.status = "approved"
                profile.moderation_note = "Automatically approved on member save."
                profile.moderated_by_id = user.id
                profile.moderated_at = datetime.now(timezone.utc)
                db.session.flush()
                db.session.add(CommunityProfileReview(
                    profile_id=profile.id,
                    moderator_user_id=user.id,
                    decision="approve",
                    note=profile.moderation_note,
                    reviewed_at=profile.moderated_at,
                ))
            db.session.commit()
            flash(
                "Profile saved and automatically approved for the directory."
                if auto_approve else
                "Profile saved privately. Directory opt-in is off.",
                "success",
            )
            return redirect(url_for("member_profile_preview"))

        return render_template(
            "member_profile_edit.html",
            title="Edit community profile",
            profile=profile,
            interests=COMMUNITY_PROFILE_INTERESTS,
            goals=COMMUNITY_PROFILE_GOALS,
            highlight_records=highlight_records,
        )

    @app.get("/member/profile/preview")
    @app.get("/member/profile/community")
    @profile_owner_required
    def member_profile_preview():
        profile = profile_query_options(CommunityProfile.query).filter_by(user_id=g.current_user.id).first()
        return render_template(
            "member_profile_preview.html",
            title="Manage Community Profile",
            profile=profile,
            public_fields=community_profile_fields(profile) if profile else {},
            eligible=has_capability(g.current_user, Capability.ADULT_DIRECTORY),
        )

    def discoverable_profile(user_id: int):
        profile = directory_query().filter(CommunityProfile.user_id == user_id).first()
        if not profile:
            abort(404)

        return profile

    def directory_query():
        return eligible_community_profiles().filter(
            ~blocked_with(g.current_user.id, CommunityProfile.user_id),
        )

    @app.get("/members/<int:user_id>/profile")
    @adult_member_required
    def community_profile_view(user_id: int):
        profile = discoverable_profile(user_id)
        connection_target = profile.user_id if profile.user_id != g.current_user.id else None
        connection = None
        if connection_target and app.config["COMMUNITY_MESSAGING_ENABLED"]:
            low, high = sorted((g.current_user.id, connection_target))
            connection = MessagePair.query.filter_by(low_id=low, high_id=high).first()
        return render_template(
            "community_profile_public.html",
            title=profile.display_name,
            public_fields=community_profile_fields(profile),
            connection_target=connection_target, connection=connection,
        )

    @app.get("/api/community/profiles/<int:user_id>")
    def community_profile_api(user_id: int):
        if not has_capability(g.current_user, Capability.ADULT_DIRECTORY):
            abort(403)
        return community_profile_fields(discoverable_profile(user_id))

    @app.get("/members/<int:user_id>/profile/photo")
    @profile_owner_required
    def community_profile_photo(user_id: int):
        profile = CommunityProfile.query.filter_by(user_id=user_id).first()
        if not profile:
            abort(404)
        owner = g.current_user.id == user_id
        reviewer = (
            has_capability(g.current_user, Capability.ADMIN_REVIEW)
            and profile.directory_opt_in
            and profile.status in {"pending", "approved"}
            and has_capability(profile.user, Capability.ADULT_DIRECTORY)
        )
        if not owner and not reviewer:
            if not has_capability(g.current_user, Capability.ADULT_DIRECTORY):
                abort(403)
            profile = discoverable_profile(user_id)
        if not owner and not profile.show_photo:
            abort(404)
        if not profile.photo:
            abort(404)
        return send_file(BytesIO(profile.photo), mimetype="image/jpeg",
                         download_name="profile.jpg", conditional=False, etag=False)

    @app.post("/member/profile/delete")
    @profile_owner_required
    def member_profile_delete():
        if request.form.get("confirm_delete") != "yes":
            abort(400)
        delete_community_profile(g.current_user.id)
        db.session.commit()
        flash("Your community profile, photo and profile review history were deleted. "
              "Your membership account is unchanged.", "success")
        return redirect(url_for("member_profile_preview"))

    @app.get("/member/resources")
    @login_required
    def member_resources():
        return render_template("member_resources.html", title="Member resources")

    @app.get("/members/search")
    @adult_member_required
    def member_search():
        result = directory_results()
        return render_template(
            "member_search.html",
            title="Member directory",
            result=result, interests=COMMUNITY_PROFILE_INTERESTS, goals=COMMUNITY_PROFILE_GOALS,
            previous_url=url_for("member_search", **dict(result["filters"], page=result["page"] - 1)),
            next_url=url_for("member_search", **dict(result["filters"], page=result["page"] + 1)),
        )

    def directory_results():
        filters = {key: request.args.get(key, "").strip() for key in ("q", "interest", "community", "goal")}
        if "interests" in request.args and not filters["interest"]:
            filters["interest"] = request.args["interests"].strip()
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            abort(400, description="Choose a valid directory page.")
        if not 1 <= page <= 10000 or any(len(value) > 120 for value in filters.values()):
            abort(400)
        if filters["interest"] and filters["interest"] not in COMMUNITY_PROFILE_INTERESTS:
            abort(400)
        if filters["goal"] and filters["goal"] not in COMMUNITY_PROFILE_GOALS:
            abort(400)
        query = directory_query()
        if filters["q"]:
            query = query.filter(CommunityProfile.display_name.icontains(filters["q"], autoescape=True))
        if filters["community"]:
            query = query.filter(CommunityProfile.show_community.is_(True),
                CommunityProfile.general_community.icontains(filters["community"], autoescape=True))
        for key, column, visible in (
            ("interest", CommunityProfile.interests, CommunityProfile.show_interests),
            ("goal", CommunityProfile.participation_goals, CommunityProfile.show_goals),
        ):
            if filters[key]:
                function = func.json_array_elements_text if db.engine.dialect.name == "postgresql" else func.json_each
                values = function(column).table_valued("value").alias()
                query = query.filter(visible.is_(True), select(1).select_from(values).where(
                    values.c.value == filters[key]).exists())
        size = 20
        rows = query.order_by(func.lower(CommunityProfile.display_name), CommunityProfile.id).offset(
            (page - 1) * size).limit(size + 1).all()
        return {"profiles": [dict(community_profile_fields(profile),
                                   profile_url=url_for("community_profile_view", user_id=profile.user_id))
                              for profile in rows[:size]],
                "page": page, "page_size": size, "has_next": len(rows) > size,
                "has_previous": page > 1, "filters": filters}

    @app.get("/api/community/profiles")
    def community_directory_api():
        if not has_capability(g.current_user, Capability.ADULT_DIRECTORY):
            abort(403)
        return directory_results()

    @app.get("/admin/members")
    @admin_required
    def admin_members():
        members = (
            User.query.filter(User.email != SOCIETY_ADMIN_EMAIL)
            .order_by(User.full_name)
            .all()
        )
        administrators = User.query.filter_by(
            is_admin=True, email=SOCIETY_ADMIN_EMAIL
        ).all()
        expired_membership = False
        for member in members:
            if not member.is_admin:
                expired_membership = (
                    _expire_membership_if_due(member) or expired_membership
                )
        if expired_membership:
            db.session.commit()
        terms = MembershipTerm.query.order_by(
            MembershipTerm.user_id, MembershipTerm.term_number
        ).all()
        latest_terms = {}
        membership_histories = {}
        for term in terms:
            latest_terms[term.user_id] = term
            membership_histories.setdefault(term.user_id, []).append(term)
        voting_eligibility = {
            member.id: _is_voting_eligible(member) for member in members
        }
        return render_template(
            "admin_members.html",
            title="Manage members",
            members=members,
            administrators=administrators,
            latest_terms=latest_terms,
            membership_histories=membership_histories,
            voting_eligibility=voting_eligibility,
        )

    @app.get("/admin/members/<int:user_id>")
    @admin_required
    def admin_member_detail(user_id: int):
        member = db.session.get(User, user_id)
        if not member:
            abort(404)
        terms = (
            MembershipTerm.query.filter_by(user_id=member.id)
            .order_by(MembershipTerm.term_number)
            .all()
        )
        return render_template(
            "admin_member_detail.html",
            title=f"Member details — {member.full_name}",
            member=member,
            terms=terms,
            voting_eligibility=_is_voting_eligible(member),
        )

    @app.post("/admin/members/<int:user_id>/make-admin")
    @admin_required
    def make_member_admin(user_id: int):
        member = db.session.get(User, user_id)
        if not member or member.is_admin or member.status != "active":
            abort(404)
        member.is_admin = True
        db.session.commit()
        flash(f"{member.full_name} is now an administrator.", "success")
        return redirect(url_for("admin_members"))

    @app.post("/admin/members/<int:user_id>/delete")
    @admin_required
    def delete_member(user_id: int):
        member = db.session.get(User, user_id)
        if not member or member.is_admin:
            abort(404)

        member_name = member.full_name
        detach_member_registrations(member.id)
        forget_network_member(member.id)
        MembershipDetails.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        MembershipPreferences.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        MembershipConsent.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        MembershipTerm.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        delete_community_profile(member.id)
        DirectoryAccessWindow.query.filter_by(user_id=member.id).delete(synchronize_session=False)
        shared_records = VerifiedCommunityRecord.query.options(selectinload(
            VerifiedCommunityRecord.participants)).filter(or_(
                VerifiedCommunityRecord.user_id == member.id,
                VerifiedCommunityRecord.participants.any(user_id=member.id))).all()
        for record in shared_records:
            remaining = [p for p in record.participants if p.user_id != member.id]
            if remaining:
                record.participants = remaining
                if record.user_id == member.id:
                    record.user_id = min(p.user_id for p in remaining)
            else:
                VerifiedCommunityRecordAudit.query.filter_by(record_id=record.id).delete(synchronize_session=False)
                CommunityProfileHighlight.query.filter_by(record_id=record.id).delete(synchronize_session=False)
                db.session.delete(record)
        db.session.flush()
        PasswordResetCode.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        anonymize_activity_member(member.id, g.current_user.id)
        db.session.delete(member)
        db.session.commit()
        flash(f"Member record for {member_name} was permanently deleted.", "success")
        return redirect(url_for("admin_members"))

    @app.get("/admin/community/profiles")
    @admin_required
    def admin_community_profiles():
        selected_status = request.args.get("status", "pending")
        if selected_status not in {"pending", "approved"}:
            abort(400)
        pending_profiles = (
            CommunityProfile.query.filter_by(status=selected_status, directory_opt_in=True)
            .order_by(CommunityProfile.updated_at)
            .all()
        )
        return render_template(
            "admin_community_profiles.html",
            title="Community profile review",
            profiles=[(profile, community_profile_fields(profile)) for profile in pending_profiles
                      if has_capability(profile.user, Capability.ADULT_DIRECTORY)],
            selected_status=selected_status,
        )

    @app.route("/admin/community/records", methods=["GET", "POST"])
    @admin_required
    def admin_community_records():
        if request.method == "POST":
            try:
                explicit_members = "user_ids" in request.form
                member_ids = set(int(value) for value in request.form.getlist(
                    "user_ids" if explicit_members else "user_id"))
                record_id = request.form.get("record_id", type=int)
                title = request.form.get("title", "").strip()
                kind = request.form.get("record_type", "")
                occurred = date.fromisoformat(request.form.get("occurred_on", ""))
                minutes = int(request.form.get("volunteer_minutes", "0")) if kind == "volunteer" else None
                if not title or len(title) > 160 or kind not in {"activity", "volunteer"}:
                    raise ValueError()
                if occurred > date.today() or (minutes is not None and not 0 < minutes <= 600000):
                    raise ValueError()
                if not member_ids or len(member_ids) > 500 or User.query.filter(User.id.in_(member_ids)).count() != len(member_ids):
                    raise ValueError()
            except ValueError:
                abort(400, description="Enter a valid member, summary, past date and activity type. Volunteer records require positive whole minutes.")
            if request.form.get("record_id") and record_id is None:
                abort(400)
            record = db.session.get(VerifiedCommunityRecord, record_id) if record_id else None
            if record_id and record is None:
                abort(404)
            if record and not explicit_members:
                if member_ids != {record.user_id}:
                    abort(400, description="Use the member list to change record participants.")
                member_ids = {participant.user_id for participant in record.participants}
            previous_members = {participant.user_id for participant in record.participants} if record else set()
            if record is None:
                record = VerifiedCommunityRecord(user_id=min(member_ids))
                db.session.add(record)
            record.user_id = min(member_ids)
            for participant in list(record.participants):
                if participant.user_id not in member_ids:
                    record.participants.remove(participant)
            existing_members = {participant.user_id for participant in record.participants}
            for member_id in member_ids - existing_members:
                record.participants.append(VerifiedCommunityRecordParticipant(user_id=member_id))
            if previous_members - member_ids:
                removed_profiles = select(CommunityProfile.id).where(
                    CommunityProfile.user_id.in_(previous_members - member_ids))
                CommunityProfileHighlight.query.filter_by(record_id=record.id).filter(
                    CommunityProfileHighlight.profile_id.in_(removed_profiles)).delete(synchronize_session=False)
            record.title, record.record_type = title, kind
            record.occurred_on, record.volunteer_minutes = occurred, minutes
            record.verified = True
            record.shareable = request.form.get("shareable") == "yes"
            record.sensitive = False
            record.verified_by_id = g.current_user.id
            record.verified_at = datetime.now(timezone.utc)
            db.session.flush()
            db.session.add(VerifiedCommunityRecordAudit(record_id=record.id, actor_id=g.current_user.id,
                snapshot={"title": title, "type": kind, "date": occurred.isoformat(),
                          "member_ids": sorted(member_ids), "previous_member_ids": sorted(previous_members),
                          "volunteer_minutes": minutes, "verified": record.verified,
                          "shareable": record.shareable, "sensitive": record.sensitive}))
            db.session.commit()
            flash("Verified record saved. Members choose whether to share eligible records.", "success")
            return redirect(url_for("admin_community_records"))
        page = request.args.get("page", 1, type=int)
        if page < 1 or page > 10000:
            abort(400)
        records = VerifiedCommunityRecord.query.options(selectinload(VerifiedCommunityRecord.participants).selectinload(
            VerifiedCommunityRecordParticipant.user)).order_by(
            VerifiedCommunityRecord.id.desc()).offset((page - 1) * 20).limit(21).all()
        members = User.query.order_by(User.full_name, User.id).all()
        return render_template("admin_community_records.html", title="Verified community records",
            records=records[:20], members=members, page=page, has_next=len(records) > 20)

    @app.post("/admin/community/profiles/<int:profile_id>/<action>")
    @admin_required
    def moderate_community_profile(profile_id: int, action: str):
        if action not in {"approve", "reject"}:
            abort(404)
        profile = CommunityProfile.query.filter_by(id=profile_id).with_for_update().first()
        if (
            not profile
            or profile.status not in ({"pending"} if action == "approve" else {"pending", "approved"})
            or not profile.directory_opt_in
        ):
            abort(404)
        if not has_capability(profile.user, Capability.ADULT_DIRECTORY):
            abort(409, description="Only active adult members may be listed.")
        if request.form.get("profile_version", type=int) != profile.version:
            abort(409, description="The profile changed. Reload and review the current version.")

        note = request.form.get("moderation_note", "").strip()
        if len(note) > 500:
            flash("Moderation note must be 500 characters or fewer.", "error")
            return redirect(url_for("admin_community_profiles"))
        profile.status = "approved" if action == "approve" else "rejected"
        profile.moderation_note = note or None
        profile.moderated_by_id = g.current_user.id
        profile.moderated_at = datetime.now(timezone.utc)
        db.session.add(
            CommunityProfileReview(
                profile_id=profile.id,
                moderator_user_id=g.current_user.id,
                decision=action,
                note=note or None,
                reviewed_at=profile.moderated_at,
            )
        )
        db.session.commit()
        flash(
            "Community profile approved." if action == "approve"
            else "Community profile rejected.",
            "success",
        )
        return redirect(url_for("admin_community_profiles"))

    @app.errorhandler(StaleDataError)
    def stale_profile_change(error):
        db.session.rollback()
        return "The profile changed. Reload it before trying again.", 409

    @app.get("/admin/requests")
    @admin_required
    def admin_requests():
        pending_users = User.query.filter_by(status="pending", is_admin=False).order_by(User.created_at).all()
        return render_template("admin_requests.html", title="Access requests", pending_users=pending_users)

    @app.post("/admin/requests/<int:user_id>/approve")
    @admin_required
    def approve_request(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status != "pending":
            abort(404)
        if request.form.get("checks_confirmed") != "yes":
            flash("Confirm that the Society's required checks are complete before approving.", "error")
            return redirect(url_for("admin_requests"))
        today = date.today()
        details = user.membership_details
        membership_type = details.membership_type if details else "legacy"
        annual_dues_cents = 1000 if membership_type == "adult" else 0
        payment_status = "paid" if annual_dues_cents else "waived"
        previous_terms = MembershipTerm.query.filter_by(user_id=user.id).all()
        term_number = max((term.term_number for term in previous_terms), default=0) + 1
        user.membership_terms.append(
            MembershipTerm(
                term_number=term_number,
                membership_type=membership_type,
                status="active",
                approval_date=today,
                start_date=today,
                expiry_date=_one_year_after(today),
                annual_dues_cents=annual_dues_cents,
                payment_status=payment_status,
                payment_date=today if annual_dues_cents else None,
                voting_eligible=(
                    _is_19_or_older(details.date_of_birth, today)
                    if details
                    else None
                ),
            )
        )
        user.status = "active"
        db.session.commit()
        try:
            _send_membership_approval_notification(user)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            request_id = error.headers.get("x-resend-id", "unknown")
            app.logger.error(
                "Resend rejected membership approval notification: HTTP %s, "
                "request id %s, response %s",
                error.code,
                request_id,
                detail,
            )
        except (OSError, ValueError):
            app.logger.exception("Could not send membership approval notification")
        flash(f"Access approved for {user.username}.", "success")
        return redirect(url_for("admin_requests"))

    @app.post("/admin/requests/<int:user_id>/decline")
    @admin_required
    def decline_request(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status != "pending":
            abort(404)
        user.status = "rejected"
        db.session.commit()
        flash("The access request was declined.", "success")
        return redirect(url_for("admin_requests"))

    @app.post("/admin/members/<int:user_id>/membership/payment")
    @admin_required
    def record_membership_payment(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin:
            abort(404)
        term = (
            MembershipTerm.query.filter_by(user_id=user.id)
            .order_by(MembershipTerm.term_number.desc())
            .first()
        )
        if _expire_membership_if_due(user):
            db.session.commit()
        if (
            not term
            or term.payment_status != "unpaid"
        ):
            abort(404)
        today = date.today()
        pending_renewal = term.status == "pending" and user.status == "inactive"
        current_term_payment = (
            term.status == "active"
            and user.status in {"active", "inactive"}
            and term.expiry_date >= today
        )
        if not pending_renewal and not current_term_payment:
            abort(404)
        payment_reference = request.form.get("payment_reference", "").strip()
        if len(payment_reference) > 255:
            flash("Payment reference must be 255 characters or fewer.", "error")
            return redirect(url_for("admin_members"))
        term.payment_status = "paid"
        term.payment_date = today
        term.payment_reference = payment_reference or None
        if current_term_payment:
            user.status = "active"
        db.session.commit()
        flash(f"Payment recorded for {user.member_number}.", "success")
        return redirect(url_for("admin_members"))

    @app.post("/admin/members/<int:user_id>/membership/renew")
    @admin_required
    def renew_membership(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status not in {"inactive", "expired"}:
            abort(404)
        today = date.today()
        pending_term = (
            MembershipTerm.query.filter_by(user_id=user.id)
            .order_by(MembershipTerm.term_number.desc())
            .first()
        )
        if (
            not pending_term
            or pending_term.status != "pending"
            or pending_term.payment_status not in {"paid", "waived"}
        ):
            abort(404)
        pending_term.status = "active"
        pending_term.approval_date = today
        pending_term.start_date = today
        pending_term.expiry_date = _one_year_after(today)
        pending_term.voting_eligible=(
            _is_19_or_older(user.membership_details.date_of_birth, today)
            if user.membership_details
            else None
        )
        user.status = "active"
        db.session.commit()
        try:
            _send_membership_approval_notification(user)
        except HTTPError as error:
            app.logger.error(
                "Resend rejected membership renewal approval: HTTP %s, request id %s",
                error.code,
                error.headers.get("x-resend-id", "unknown"),
            )
        except (OSError, ValueError):
            app.logger.exception("Could not send membership renewal approval")
        flash(f"Membership renewed for {user.member_number}.", "success")
        return redirect(url_for("admin_members"))

    @app.post("/admin/members/<int:user_id>/membership/renew/decline")
    @admin_required
    def decline_membership_renewal(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status not in {"inactive", "expired"}:
            abort(404)
        latest_term = (
            MembershipTerm.query.filter_by(user_id=user.id)
            .order_by(MembershipTerm.term_number.desc())
            .first()
        )
        if not latest_term or latest_term.status != "pending":
            abort(404)
        latest_term.status = "declined"
        db.session.commit()
        flash("The membership renewal request was declined.", "success")
        return redirect(url_for("admin_members"))

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    def create_admin_command(username: str) -> None:
        """Create the first administrator using an interactive password prompt."""
        normalized_username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,40}", normalized_username):
            raise click.ClickException("Username must be 3–40 letters, numbers, dots, underscores, or hyphens.")
        if User.query.filter_by(username=normalized_username).first():
            raise click.ClickException("That username already exists.")
        email = click.prompt("Administrator email").strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            raise click.ClickException("Enter a valid email address.")
        password = click.prompt("Administrator password", hide_input=True, confirmation_prompt=True)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        administrator = User(
            full_name="Society administrator",
            email=email,
            username=normalized_username,
            status="active",
            is_admin=True,
        )
        administrator.set_password(password)
        db.session.add(administrator)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise click.ClickException("That email address already has an account.") from error
        click.echo("Administrator account created.")

    register_activities(app, User=User, CommunityProfile=CommunityProfile, MembershipDetails=MembershipDetails,
                        MembershipTerm=MembershipTerm, login_required=login_required,
                        admin_required=admin_required)
    register_messaging(
        app, User=User, CommunityProfile=CommunityProfile,
        profile_discoverable=discoverable_profile, send_email=lambda **fields: _send_resend_email(**fields),
    )
    return app


app = create_app()
