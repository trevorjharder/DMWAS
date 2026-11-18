"""Activity proposals, revisions, reviews and co-organizers.

Revision ID: f06c3d4e5f6a
Revises: e05b2c3d4e5f
"""
from alembic import op
import sqlalchemy as sa


revision = "f06c3d4e5f6a"
down_revision = "e05b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organizer_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("classification", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("classification IN ('member', 'official')", name="ck_activity_classification"),
        sa.CheckConstraint("status IN ('draft', 'submitted', 'changes_requested', "
                           "'rejected', 'approved', 'unpublished')", name="ck_activity_status"),
    )
    op.create_index("ix_activity_organizer_id", "activity", ["organizer_id"])
    op.create_index("ix_activity_status_id", "activity", ["status", "id"])
    op.create_table(
        "activity_revision",
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activity.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("public_details", sa.JSON(), nullable=False),
        sa.Column("organizer_details", sa.JSON(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "activity_coorganizer",
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activity.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.CheckConstraint("status IN ('invited', 'accepted', 'declined', 'approved', 'revoked')",
                           name="ck_activity_coorganizer_status"),
    )
    op.create_index("ix_activity_coorganizer_user_id", "activity_coorganizer", ["user_id"])
    op.create_table(
        "activity_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activity_id", sa.Integer(), sa.ForeignKey("activity.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("subject_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("previous_status", sa.String(20)),
        sa.Column("new_status", sa.String(20), nullable=False),
        sa.Column("note", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("activity_id", "actor_id", "subject_id"):
        op.create_index(f"ix_activity_audit_{field}", "activity_audit", [field])
    op.create_table(
        "activity_action_window",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("window_number", sa.BigInteger(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("activity_action_window")
    op.drop_table("activity_audit")
    op.drop_table("activity_coorganizer")
    op.drop_table("activity_revision")
    op.drop_table("activity")
