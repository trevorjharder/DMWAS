"""District of Mount Waddington Access Society website."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
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
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()
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
    "membership_support": "membership-support-v1",
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


def _one_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def _expire_membership_if_due(user: User, today: date | None = None) -> bool:
    if user.status != "active":
        return False
    latest_term = (
        MembershipTerm.query.filter_by(user_id=user.id)
        .order_by(MembershipTerm.term_number.desc())
        .first()
    )
    if not latest_term or (today or date.today()) <= latest_term.expiry_date:
        return False
    latest_term.status = "expired"
    user.status = "expired"
    return True


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
    return user.status == "active" and _is_19_or_older(
        user.membership_details.date_of_birth, today
    )


def _database_uri() -> str:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///dmwas.db")
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return database_url


def _password_reset_code_hash(secret_key: str, email: str, code: str) -> str:
    """Hash a low-entropy code with the app secret so database leaks don't expose it."""
    message = f"{email.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _send_resend_email(recipient: str, subject: str, text: str) -> None:
    """Send a plain-text email through Resend's HTTPS API."""
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_FROM") or os.environ.get("SMTP_FROM")
    if not api_key or not sender:
        logging.getLogger(__name__).info(
            "Resend is not configured; skipped email delivery."
        )
        return

    payload = json.dumps(
        {"from": sender, "to": [recipient], "subject": subject, "text": text}
    ).encode("utf-8")
    resend_request = urllib_request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib_request.urlopen(resend_request, timeout=10) as response:
        response.read()


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


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(__name__)
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key and (
        os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("ENVIRONMENT", "").lower() == "production"
    ):
        raise RuntimeError("SECRET_KEY must be configured in production.")
    app.config.from_mapping(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        SQLALCHEMY_DATABASE_URI=_database_uri(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        PUBLIC_CONTACT_EMAIL=os.environ.get("PUBLIC_CONTACT_EMAIL", ""),
        PUBLIC_CONTACT_PHONE=os.environ.get("PUBLIC_CONTACT_PHONE", ""),
        PUBLIC_CONTACT_ADDRESS=os.environ.get("PUBLIC_CONTACT_ADDRESS", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
        == "true",
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    schema_lock = threading.Lock()
    schema_initialized = False

    @app.before_request
    def initialize_missing_tables() -> None:
        nonlocal schema_initialized
        if schema_initialized:
            return
        with schema_lock:
            if schema_initialized:
                return
            if db.engine.dialect.name == "postgresql":
                # Serialize first-request setup across Gunicorn worker processes.
                with db.engine.begin() as connection:
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": 4839628471201},
                    )
                    db.metadata.create_all(bind=connection)
            else:
                db.create_all()
            schema_initialized = True

    @app.before_request
    def protect_post_requests() -> None:
        if request.method != "POST":
            return
        expected_token = session.get("csrf_token")
        submitted_token = request.form.get("csrf_token", "")
        if not expected_token or not secrets.compare_digest(expected_token, submitted_token):
            abort(400, description="The form expired or was invalid. Please try again.")

    @app.before_request
    def load_current_user() -> None:
        user_id = session.get("user_id")
        g.current_user = db.session.get(User, user_id) if user_id else None
        user = g.current_user
        if user and _expire_membership_if_due(user):
            db.session.commit()

    @app.context_processor
    def inject_template_values() -> dict[str, object]:
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"current_user": g.get("current_user")}

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    def login_required(view: F) -> F:
        @wraps(view)
        def wrapped(*args: object, **kwargs: object):
            user = g.get("current_user")
            if not user or user.status != "active":
                session.clear()
                flash("Please sign in with an approved member account.", "info")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return cast(F, wrapped)

    def admin_required(view: F) -> F:
        @wraps(view)
        @login_required
        def wrapped(*args: object, **kwargs: object):
            if not g.current_user.is_admin:
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
    def contact():
        return render_template("contact.html", title="Contact")

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", title="Privacy information")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and _expire_membership_if_due(user):
                db.session.commit()
            if user and user.status == "active" and user.check_password(password):
                session.clear()
                session["user_id"] = user.id
                flash("You are signed in.", "success")
                destination = request.args.get("next", "")
                if destination.startswith("/") and not destination.startswith("//"):
                    return redirect(destination)
                return redirect(url_for("member_home"))
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
            flash("Your membership application has been received. Sign-in will be available after Society approval.", "success")
            return redirect(url_for("login"))

        return render_template("membership_request.html", title="Membership application")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        flash("You are signed out.", "success")
        return redirect(url_for("about"))

    @app.get("/member")
    @login_required
    def member_home():
        return render_template("member_home.html", title="Member area")

    @app.get("/member/resources")
    @login_required
    def member_resources():
        return render_template("member_resources.html", title="Member resources")

    @app.get("/members/search")
    @login_required
    def member_search():
        query = request.args.get("q", "").strip()
        members = []
        if query:
            members = (
                User.query.filter_by(status="active", is_admin=False)
                .filter(User.full_name.ilike(f"%{query}%"))
                .order_by(User.full_name)
                .limit(50)
                .all()
            )
        return render_template(
            "member_search.html", title="Search members", query=query, members=members
        )

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
        if not member or member.is_admin:
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
        PasswordResetCode.query.filter_by(user_id=member.id).delete(
            synchronize_session=False
        )
        db.session.delete(member)
        db.session.commit()
        flash(f"Member record for {member_name} and its associated data were permanently deleted.", "success")
        return redirect(url_for("admin_members"))

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
        payment_status = "unpaid" if annual_dues_cents else "waived"
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
                voting_eligible=(
                    _is_19_or_older(details.date_of_birth, today)
                    if details
                    else None
                ),
            )
        )
        user.status = "active"
        db.session.commit()
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
            or term.status != "active"
            or user.status != "active"
            or term.payment_status != "unpaid"
        ):
            abort(404)
        payment_reference = request.form.get("payment_reference", "").strip()
        if len(payment_reference) > 255:
            flash("Payment reference must be 255 characters or fewer.", "error")
            return redirect(url_for("admin_members"))
        term.payment_status = "paid"
        term.payment_date = date.today()
        term.payment_reference = payment_reference or None
        db.session.commit()
        flash(f"Payment recorded for {user.member_number}.", "success")
        return redirect(url_for("admin_members"))

    @app.post("/admin/members/<int:user_id>/membership/renew")
    @admin_required
    def renew_membership(user_id: int):
        user = db.session.get(User, user_id)
        if not user or user.is_admin or user.status != "expired":
            abort(404)
        today = date.today()
        prior_terms = MembershipTerm.query.filter_by(user_id=user.id).all()
        latest_term = max(prior_terms, key=lambda term: term.term_number, default=None)
        if not latest_term:
            abort(404)
        membership_type = latest_term.membership_type
        annual_dues_cents = 1000 if membership_type == "adult" else 0
        user.membership_terms.append(
            MembershipTerm(
                term_number=latest_term.term_number + 1,
                membership_type=membership_type,
                status="active",
                approval_date=today,
                start_date=today,
                expiry_date=_one_year_after(today),
                annual_dues_cents=annual_dues_cents,
                payment_status="unpaid" if annual_dues_cents else "waived",
                voting_eligible=(
                    _is_19_or_older(user.membership_details.date_of_birth, today)
                    if user.membership_details
                    else None
                ),
            )
        )
        user.status = "active"
        db.session.commit()
        flash(f"Membership renewed for {user.member_number}.", "success")
        return redirect(url_for("admin_members"))

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the database tables for a new installation."""
        with app.app_context():
            db.create_all()
        click.echo("Database tables created.")

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

    return app


app = create_app()
