"""Remove community profile highlights and their source records.

Revision ID: c09f6a7b8c9d
Revises: b08e5f6a7b8c
"""
from alembic import op
import sqlalchemy as sa

revision = "c09f6a7b8c9d"
down_revision = "b08e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("community_profile_highlight")
    op.drop_table("verified_community_record_audit")
    op.drop_table("verified_community_record_participant")
    op.drop_table("verified_community_record")
    op.drop_column("community_profile", "show_highlights")


def downgrade():
    # Restore the old schema without restoring deleted records or selections.
    op.add_column("community_profile", sa.Column(
        "show_highlights", sa.Boolean(), server_default=sa.false(), nullable=False,
    ))
    op.create_table(
        "verified_community_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("record_type", sa.String(20), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("volunteer_minutes", sa.Integer()),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("shareable", sa.Boolean(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), nullable=False),
        sa.Column("verified_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_verified_community_record_user_id", "verified_community_record", ["user_id"])
    op.create_table(
        "community_profile_highlight",
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("community_profile.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("verified_community_record.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "verified_community_record_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("verified_community_record.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_verified_community_record_audit_record_id", "verified_community_record_audit", ["record_id"])
    op.create_table(
        "verified_community_record_participant",
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("verified_community_record.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_verified_community_record_participant_user_id",
                    "verified_community_record_participant", ["user_id"])
