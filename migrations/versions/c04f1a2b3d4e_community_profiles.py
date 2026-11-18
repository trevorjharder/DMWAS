"""Community profiles and profile review history.

Revision ID: c04f1a2b3d4e
Revises: 958ef19b9634
"""
from alembic import op
import sqlalchemy as sa

revision = "c04f1a2b3d4e"
down_revision = "958ef19b9634"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "community_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("biography", sa.String(500), nullable=False),
        sa.Column("general_community", sa.String(120), nullable=False),
        sa.Column("interests", sa.JSON(), nullable=False),
        sa.Column("participation_goals", sa.JSON(), nullable=False),
        sa.Column("directory_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_biography", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_community", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_interests", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("show_goals", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("photo", sa.LargeBinary(), nullable=True),
        sa.Column("show_photo", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("moderation_note", sa.String(500), nullable=True),
        sa.Column("moderated_by_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_community_profile_status", "community_profile", ["status"])
    op.create_table(
        "community_profile_review",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("community_profile.id", ondelete="CASCADE"), nullable=False),
        sa.Column("moderator_user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_community_profile_review_profile_id", "community_profile_review", ["profile_id"])


def downgrade():
    # Drops only Phase 04 data. Back up first; membership tables are preserved.
    op.drop_table("community_profile_review")
    op.drop_table("community_profile")
