from datetime import datetime, timezone

from extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ActivityRegistration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id", ondelete="CASCADE"),
                            nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    display_name = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(16), nullable=False)
    queued_at = db.Column(db.DateTime(timezone=True))
    offer_expires_at = db.Column(db.DateTime(timezone=True))
    renewal_until = db.Column(db.DateTime(timezone=True))
    start_checked = db.Column(db.Boolean, nullable=False, default=False)
    attendance = db.Column(db.String(10))
    attendance_at = db.Column(db.DateTime(timezone=True))
    attendance_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"),
                                 index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now,
                           onupdate=utc_now)

    __table_args__ = (
        db.UniqueConstraint("activity_id", "user_id", name="uq_activity_registration_member"),
        db.CheckConstraint(
            "status IN ('requested','waitlisted','offered','confirmed','cancelled','declined','expired')",
            name="ck_activity_registration_status"),
        db.CheckConstraint("attendance IN ('attended','absent')", name="ck_activity_attendance"),
        db.Index("ix_activity_registration_queue", "activity_id", "status", "queued_at", "id"),
    )


class ActivityVolunteerHours(db.Model):
    registration_id = db.Column(
        db.Integer, db.ForeignKey("activity_registration.id", ondelete="CASCADE"), primary_key=True)
    version = db.Column(db.Integer, primary_key=True)
    minutes = db.Column(db.Integer, nullable=False)
    submission_note = db.Column(db.String(500))
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"),
                                index=True)
    status = db.Column(db.String(10), nullable=False, default="pending")
    reviewed_at = db.Column(db.DateTime(timezone=True))
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"),
                               index=True)
    review_note = db.Column(db.String(500))

    __table_args__ = (
        db.CheckConstraint("minutes BETWEEN 0 AND 600000", name="ck_activity_volunteer_minutes"),
        db.CheckConstraint("status IN ('pending','verified','rejected')",
                           name="ck_activity_volunteer_status"),
        db.Index("ix_activity_volunteer_status", "status", "registration_id"),
    )


class ActivityNotice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    event = db.Column(db.String(24), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (db.Index("ix_activity_notice_user_id_id", "user_id", "id"),)


def anonymize_registration_member(user_id):
    ActivityNotice.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    for registration in ActivityRegistration.query.filter_by(user_id=user_id):
        registration.user_id = None
        registration.display_name = ""
        registration.renewal_until = None
        if not registration.start_checked:
            registration.status = "cancelled"
            registration.offer_expires_at = None
    for model, field in (
        (ActivityRegistration, ActivityRegistration.attendance_by_id),
        (ActivityVolunteerHours, ActivityVolunteerHours.submitted_by_id),
        (ActivityVolunteerHours, ActivityVolunteerHours.reviewed_by_id),
    ):
        model.query.filter(field == user_id).update({field: None}, synchronize_session=False)
