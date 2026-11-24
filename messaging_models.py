"""Storage for opt-in networking, private conversations and report evidence."""
from datetime import datetime, timezone

from extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class MessagePair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    low_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    high_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    requested_by_low = db.Column(db.Boolean, nullable=False)
    status = db.Column(db.String(16), nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True))
    low_blocked = db.Column(db.Boolean, nullable=False, default=False)
    high_blocked = db.Column(db.Boolean, nullable=False, default=False)
    low_read_through = db.Column(db.Integer, nullable=False, default=0)
    high_read_through = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        db.UniqueConstraint("low_id", "high_id", name="uq_message_pair_members"),
        db.CheckConstraint("low_id < high_id", name="ck_message_pair_order"),
        db.CheckConstraint("status IN ('pending','accepted','declined','disconnected')",
                           name="ck_message_pair_status"),
    )

    @property
    def blocked(self):
        return self.low_blocked or self.high_blocked

    def peer_id(self, user_id):
        return self.high_id if self.low_id == user_id else self.low_id


class PrivateMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pair_id = db.Column(db.Integer, db.ForeignKey("message_pair.id", ondelete="CASCADE"),
                        nullable=False)
    sent_by_low = db.Column(db.Boolean, nullable=False)
    body = db.Column(db.String(2000), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    __table_args__ = (
        db.Index("ix_private_message_pair_id_id", "pair_id", "id"),
        {"sqlite_autoincrement": True},
    )


class MessageReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pair_id = db.Column(db.Integer, db.ForeignKey("message_pair.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    reason = db.Column(db.String(24), nullable=False)
    note = db.Column(db.String(800))
    status = db.Column(db.String(12), nullable=False, default="open")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    closed_at = db.Column(db.DateTime(timezone=True))

    __table_args__ = (
        db.CheckConstraint("status IN ('open','resolved','dismissed')", name="ck_message_report_status"),
        db.Index("ix_message_report_status_id", "status", "id"),
    )


class MessageEvidence(db.Model):
    report_id = db.Column(db.Integer, db.ForeignKey("message_report.id", ondelete="CASCADE"),
                          primary_key=True)
    original_message_id = db.Column(db.Integer, primary_key=True)
    from_reporter = db.Column(db.Boolean, nullable=False)
    body = db.Column(db.String(2000), nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True), nullable=False)


class MessageReportEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey("message_report.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(12), nullable=False)
    note = db.Column(db.String(800))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class MessagingPreference(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    digest_enabled = db.Column(db.Boolean, nullable=False, default=False)


class MessageDigest(db.Model):
    """One delivery snapshot per recipient, reused until a dispatch completes."""
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    through_id = db.Column(db.Integer, nullable=False)
    unread_count = db.Column(db.Integer, nullable=False)
    prepared_at = db.Column(db.DateTime(timezone=True), nullable=False)
    claimed_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))


class MessagingQuota(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    bucket = db.Column(db.String(12), primary_key=True)
    window_number = db.Column(db.BigInteger, nullable=False)
    requests = db.Column(db.Integer, nullable=False)
