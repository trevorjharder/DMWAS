"""Activity registration, notices, attendance and volunteer hours.

Revision ID: a07d4e5f6a7b
Revises: f06c3d4e5f6a
"""
from alembic import op
import sqlalchemy as sa


revision = "a07d4e5f6a7b"
down_revision = "f06c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "activity_registration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activity.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("offer_expires_at", sa.DateTime(timezone=True)),
        sa.Column("renewal_until", sa.DateTime(timezone=True)),
        sa.Column("start_checked", sa.Boolean(), nullable=False),
        sa.Column("attendance", sa.String(10)),
        sa.Column("attendance_at", sa.DateTime(timezone=True)),
        sa.Column("attendance_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("activity_id", "user_id", name="uq_activity_registration_member"),
        sa.CheckConstraint(
            "status IN ('requested','waitlisted','offered','confirmed','cancelled','declined','expired')",
            name="ck_activity_registration_status"),
        sa.CheckConstraint("attendance IN ('attended','absent')", name="ck_activity_attendance"),
    )
    op.create_index("ix_activity_registration_user_id", "activity_registration", ["user_id"])
    op.create_index("ix_activity_registration_attendance_by_id", "activity_registration",
                    ["attendance_by_id"])
    op.create_index("ix_activity_registration_queue", "activity_registration",
                    ["activity_id", "status", "queued_at", "id"])
    op.create_table(
        "activity_volunteer_hours",
        sa.Column("registration_id", sa.Integer(),
                  sa.ForeignKey("activity_registration.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("submission_note", sa.String(500)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("review_note", sa.String(500)),
        sa.CheckConstraint("minutes BETWEEN 0 AND 600000", name="ck_activity_volunteer_minutes"),
        sa.CheckConstraint("status IN ('pending','verified','rejected')",
                           name="ck_activity_volunteer_status"),
    )
    for actor in ("submitted_by_id", "reviewed_by_id"):
        op.create_index(f"ix_activity_volunteer_hours_{actor}", "activity_volunteer_hours", [actor])
    op.create_index("ix_activity_volunteer_status", "activity_volunteer_hours",
                    ["status", "registration_id"])
    op.create_table(
        "activity_notice",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activity.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_activity_notice_activity_id", "activity_notice", ["activity_id"])
    op.create_index("ix_activity_notice_user_id_id", "activity_notice", ["user_id", "id"])


def downgrade():
    op.drop_table("activity_notice")
    op.drop_table("activity_volunteer_hours")
    op.drop_table("activity_registration")
