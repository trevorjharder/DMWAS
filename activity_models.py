from datetime import datetime, timezone

from extensions import db


class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    organizer_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"),
                             index=True)
    classification = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    revision_number = db.Column(db.Integer, nullable=False, default=1)
    lock_version = db.Column(db.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": lock_version}
    __table_args__ = (
        db.CheckConstraint("classification IN ('member', 'official')",
                           name="ck_activity_classification"),
        db.CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', "
                           "'rejected', 'approved', 'unpublished')", name="ck_activity_status"),
        db.Index("ix_activity_status_id", "status", "id"),
    )

    def touch(self):
        """Also serialize mutations that only change a child row."""
        self.lock_version += 1


class ActivityRevision(db.Model):
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id", ondelete="CASCADE"),
                            primary_key=True)
    number = db.Column(db.Integer, primary_key=True)
    public_details = db.Column(db.JSON, nullable=False)
    organizer_details = db.Column(db.JSON, nullable=False)
    starts_at = db.Column(db.DateTime(timezone=True))
    ends_at = db.Column(db.DateTime(timezone=True))


class ActivityCoorganizer(db.Model):
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id", ondelete="CASCADE"),
                            primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"),
                        primary_key=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="invited")

    __table_args__ = (
        db.CheckConstraint("status IN ('invited', 'accepted', 'declined', 'approved', 'revoked')",
                           name="ck_activity_coorganizer_status"),
    )


class ActivityAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    revision_number = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(30), nullable=False)
    previous_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20), nullable=False)
    note = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))


class ActivityActionWindow(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"),
                        primary_key=True)
    window_number = db.Column(db.BigInteger, nullable=False)
    requests = db.Column(db.Integer, nullable=False)


def anonymize_activity_member(user_id, actor_id):
    """Detach deleted accounts, including on SQLite with FK enforcement off."""
    for activity in Activity.query.filter_by(organizer_id=user_id):
        new_status = ("unpublished" if activity.status in {"approved", "submitted"}
                      else activity.status)
        db.session.add(ActivityAudit(
            activity_id=activity.id, actor_id=actor_id, revision_number=activity.revision_number,
            action="organizer_removed", previous_status=activity.status, new_status=new_status,
        ))
        activity.organizer_id = None
        activity.status = new_status
    memberships = db.session.query(Activity, ActivityCoorganizer).join(
        ActivityCoorganizer, ActivityCoorganizer.activity_id == Activity.id,
    ).filter(ActivityCoorganizer.user_id == user_id)
    for activity, invitation in memberships:
        activity.touch()
        db.session.add(ActivityAudit(
            activity_id=activity.id, actor_id=actor_id, revision_number=activity.revision_number,
            action="coorganizer_removed", previous_status=invitation.status, new_status="removed",
        ))
        db.session.delete(invitation)
    ActivityActionWindow.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    ActivityAudit.query.filter_by(actor_id=user_id).update(
        {"actor_id": None}, synchronize_session=False)
    ActivityAudit.query.filter_by(subject_id=user_id).update(
        {"subject_id": None}, synchronize_session=False)
