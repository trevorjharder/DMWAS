"""Seat assignment and activity registration policy."""
from dataclasses import dataclass
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import update
from sqlalchemy.orm import selectinload

from activity_forms import ACTIVITY_TIME_ZONE
from activity_models import Activity, ActivityAudit, ActivityCoorganizer, ActivityRevision
from extensions import db
from permissions import is_active_member, is_adult_member, is_adult_member_for_history
from registration_models import (
    ActivityNotice, ActivityRegistration, ActivityVolunteerHours, anonymize_registration_member,
    utc_now,
)


OPEN_STATUSES = ("requested", "waitlisted", "offered", "confirmed")
SEAT_STATUSES = ("offered", "confirmed")
STATUS_LABELS = {
    "requested": "Awaiting organizer approval", "waitlisted": "On the waitlist",
    "offered": "Place offered", "confirmed": "Confirmed", "cancelled": "Cancelled",
    "declined": "Declined", "expired": "Expired",
}
NOTICE_TEXT = {
    "requested": "Your registration was sent for organizer approval.",
    "organizer_request": "A new registration is waiting for organizer approval.",
    "waitlisted": "You joined the activity waitlist.",
    "offered": "A place is available. Accept it from My registrations before the offer expires.",
    "offer_expired": "Your waitlist offer expired.",
    "confirmed": "Your registration is confirmed.",
    "cancelled": "Your registration was cancelled.",
    "declined": "The organizer declined your registration request.",
    "activity_paused": "Activity details have changed. Registration is paused while the changes are reviewed.",
    "activity_updated": "The organizer updated the activity. Check the new date, time and meeting details.",
    "activity_cancelled": "The activity was cancelled. Your registration or waitlist place was released.",
    "renewal_due": "Renew your membership before the renewal deadline shown in My registrations.",
    "membership_expired": "Your place was released because your membership requirements were not met.",
    "registration_closed": "Registration closed before your request or waitlist place was confirmed.",
    "hours_verified": "Your volunteer hours were verified.",
    "hours_rejected": "Your volunteer hours were returned to the organizer for review.",
}


@dataclass(frozen=True)
class RegistrationPolicy:
    offer_hours: int = 24
    signup_close_minutes: int = 60
    cancellation_close_minutes: int = 60
    renewal_grace_hours: int = 24
    allow_youth: bool = False


def as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lock_activity(activity_id):
    """Serialize activity and roster mutations on PostgreSQL and SQLite."""
    activity_id = db.session.execute(
        update(Activity).where(Activity.id == activity_id)
        .values(lock_version=Activity.lock_version)
        .returning(Activity.id).execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if activity_id is None:
        return None
    return db.session.get(Activity, activity_id, populate_existing=True)


def registration_notice(user_id, activity_id, event, now=None):
    if user_id is not None:
        db.session.add(ActivityNotice(
            user_id=user_id, activity_id=activity_id, event=event, created_at=now or utc_now(),
        ))


def change_status(registration, status, event=None, now=None):
    registration.status = status
    if status != "offered":
        registration.offer_expires_at = None
    if status not in OPEN_STATUSES:
        registration.renewal_until = None
    if event:
        registration_notice(registration.user_id, registration.activity_id, event, now)


def update_registrations_for_activity(activity, change, now=None):
    """Keep roster changes and notifications in the same transaction as the proposal."""
    now = now or utc_now()
    for registration in ActivityRegistration.query.filter(
        ActivityRegistration.activity_id == activity.id, ActivityRegistration.status.in_(OPEN_STATUSES),
        ActivityRegistration.start_checked.is_(False),
    ):
        if change == "activity_cancelled":
            change_status(registration, "cancelled", change, now)
        else:
            if change == "activity_paused" and registration.status == "offered":
                change_status(registration, "waitlisted")
            registration_notice(registration.user_id, activity.id, change, now)


def detach_member_registrations(user_id):
    """Release places for removed accounts and anonymize past attendance."""
    own_activities = db.select(Activity.id).where(Activity.organizer_id == user_id)
    registrations = db.select(ActivityRegistration.activity_id).where(
        ActivityRegistration.user_id == user_id)
    activity_ids = db.session.execute(own_activities.union(registrations).order_by("id")).scalars().all()
    for activity_id in activity_ids:
        activity = lock_activity(activity_id)
        if activity and activity.organizer_id == user_id:
            update_registrations_for_activity(activity, "activity_cancelled")
    anonymize_registration_member(user_id)


def latest_verified_hours(registration_ids):
    if not registration_ids:
        return {}
    latest = db.select(
        ActivityVolunteerHours.registration_id,
        db.func.max(ActivityVolunteerHours.version).label("version"),
    ).where(ActivityVolunteerHours.registration_id.in_(registration_ids),
            ActivityVolunteerHours.status == "verified").group_by(
                ActivityVolunteerHours.registration_id).subquery()
    return dict(db.session.query(ActivityVolunteerHours.registration_id, ActivityVolunteerHours.minutes)
                .join(latest, (ActivityVolunteerHours.registration_id == latest.c.registration_id)
                      & (ActivityVolunteerHours.version == latest.c.version)).all())


def approved_revision_join():
    last_approved = db.select(db.func.max(ActivityAudit.revision_number)).where(
        ActivityAudit.activity_id == Activity.id, ActivityAudit.action == "approve",
    ).correlate(Activity).scalar_subquery()
    return db.and_(
        ActivityRevision.activity_id == Activity.id,
        ActivityRevision.number == db.case(
            (Activity.status == "approved", Activity.revision_number), else_=last_approved),
    )


class RegistrationService:
    def __init__(self, *, User, activity, policy, organizer_eligible, member_publication, now=None):
        self.User = User
        self.activity = activity
        self.policy = policy
        self.now = now or utc_now()
        self.revision = db.session.get(ActivityRevision, (activity.id, activity.revision_number))
        self.approved_revision = self.revision if activity.status == "approved" else (
            ActivityRevision.query.join(Activity, approved_revision_join()).filter(
                Activity.id == activity.id).first()
        )
        scheduled = self.approved_revision or self.revision
        self.start = as_utc(scheduled.starts_at) if scheduled.starts_at else None
        self.end = as_utc(scheduled.ends_at) if scheduled.ends_at else None
        self.zone = ZoneInfo(scheduled.public_details.get("time_zone") or ACTIVITY_TIME_ZONE)
        self.signup_closes = self.start - timedelta(minutes=policy.signup_close_minutes) if self.start else None
        self.cancellation_closes = (
            self.start - timedelta(minutes=policy.cancellation_close_minutes) if self.start else None)
        organizer = db.session.get(User, activity.organizer_id) if activity.organizer_id else None
        self.is_live = (activity.status == "approved"
                        and (activity.classification == "official" or member_publication)
                        and organizer_eligible(organizer, activity.classification))

    @property
    def signup_open(self):
        return self.is_live and self.signup_closes is not None and self.now < self.signup_closes

    @property
    def capacity(self):
        return int(self.revision.organizer_details["capacity"])

    def eligible(self, user, moment=None):
        day = (moment or self.now).astimezone(self.zone).date()
        check = is_active_member if self.policy.allow_youth else is_adult_member
        return check(user, day)

    def eligible_at_start(self, user):
        day = self.start.astimezone(self.zone).date()
        if self.now.astimezone(self.zone).date() <= day or (user and not user.membership_terms):
            return self.eligible(user, self.start)
        if not user or user.status not in {"active", "inactive", "expired"}:
            return False
        # Late attendance entry can use the paid or waived term covering the activity day.
        return (self.policy.allow_youth or is_adult_member_for_history(user, day)) and any(
            term.start_date <= day <= term.expiry_date and term.approval_date <= day
            and term.status in {"active", "inactive", "expired"} and term.payment_status in {"paid", "waived"}
            for term in user.membership_terms
        )

    def registrations(self, *statuses):
        return ActivityRegistration.query.filter_by(activity_id=self.activity.id).filter(
            ActivityRegistration.status.in_(statuses) if statuses else db.true())

    def available_seats(self):
        return self.capacity - self.registrations(*SEAT_STATUSES).count()

    def reconcile(self):
        players = self.registrations(*OPEN_STATUSES).filter(
            ActivityRegistration.start_checked.is_(False),
        ).order_by(ActivityRegistration.queued_at, ActivityRegistration.id).all()
        if not players or not self.start:
            return
        # Paused activities keep their registrations until approval or the start deadline.
        if not self.is_live and self.now < self.start:
            return
        if not self.is_live:
            for registration in players:
                change_status(registration, "cancelled", "activity_cancelled", self.now)
            return
        users = {
            user.id: user for user in self.User.query.join(
                ActivityRegistration, ActivityRegistration.user_id == self.User.id,
            ).filter(ActivityRegistration.activity_id == self.activity.id,
                     ActivityRegistration.status.in_(OPEN_STATUSES)).options(
                selectinload(self.User.membership_details), selectinload(self.User.membership_terms),
            )
        }
        waitlist = []
        for registration in players:
            user = users.get(registration.user_id)
            if self.now >= self.start and registration.status == "confirmed":
                if self.eligible_at_start(user):
                    registration.start_checked = True
                    registration.renewal_until = None
                else:
                    change_status(registration, "expired", "membership_expired", self.now)
                continue
            if registration.status == "offered" and self.now >= as_utc(registration.offer_expires_at):
                change_status(registration, "expired", "offer_expired", self.now)
                continue
            if registration.status != "confirmed" and not self.signup_open:
                change_status(registration, "expired", "registration_closed", self.now)
                continue
            check_time = self.start if self.now >= self.signup_closes else self.now
            if not self.eligible(user, check_time):
                self.handle_renewal(registration)
                continue
            registration.renewal_until = None
            if registration.status == "waitlisted":
                waitlist.append(registration)
        if self.signup_open and waitlist:
            self.promote(waitlist)

    def handle_renewal(self, registration):
        if self.policy.renewal_grace_hours and registration.renewal_until is None:
            registration.renewal_until = min(
                self.start, self.now + timedelta(hours=self.policy.renewal_grace_hours),
            )
            registration_notice(registration.user_id, self.activity.id, "renewal_due", self.now)
        until = registration.renewal_until
        if until is None or self.now >= as_utc(until):
            change_status(registration, "expired", "membership_expired", self.now)

    def promote(self, waitlist):
        available = self.available_seats()
        deadline = min(self.signup_closes, self.now + timedelta(hours=self.policy.offer_hours))
        for registration in waitlist[:max(0, available)]:
            registration.offer_expires_at = deadline
            change_status(registration, "offered", "offered", self.now)

    def allocate(self, registration):
        if self.available_seats() > 0:
            change_status(registration, "confirmed", "confirmed", self.now)
        else:
            registration.queued_at = self.now
            change_status(registration, "waitlisted", "waitlisted", self.now)

    def notify_organizers_of_request(self):
        approved = ActivityCoorganizer.query.filter_by(activity_id=self.activity.id, status="approved")
        organizer_id = self.activity.organizer_id
        registration_notice(organizer_id, self.activity.id, "organizer_request", self.now)
        for row in approved:
            if row.user_id != organizer_id and is_adult_member(db.session.get(self.User, row.user_id)):
                registration_notice(row.user_id, self.activity.id, "organizer_request", self.now)
