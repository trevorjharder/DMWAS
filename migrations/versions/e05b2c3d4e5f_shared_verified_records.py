"""Member lists for shared verified community records.

Revision ID: e05b2c3d4e5f
Revises: d05a1b2c3d4e
"""
from alembic import op
import sqlalchemy as sa

revision = "e05b2c3d4e5f"
down_revision = "d05a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade():
    participants = op.create_table(
        "verified_community_record_participant",
        sa.Column("record_id", sa.Integer(), sa.ForeignKey("verified_community_record.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_verified_community_record_participant_user_id", participants.name, ["user_id"])
    # Existing records keep their member and existing profile selections.
    source = sa.table("verified_community_record", sa.column("id"), sa.column("user_id"))
    op.get_bind().execute(participants.insert().from_select(["record_id", "user_id"],
                                                          sa.select(source.c.id, source.c.user_id)))


def downgrade():
    # A Phase 05 application supports only its primary participant. Refuse a
    # rollback that would silently remove other members' verified contributions.
    connection = op.get_bind()
    groups = connection.execute(sa.text(
        "SELECT record_id FROM verified_community_record_participant "
        "GROUP BY record_id HAVING count(*) > 1 LIMIT 1")).first()
    if groups:
        raise RuntimeError("Shared verified records exist. Preserve a backup and resolve multi-member records before downgrading.")
    op.drop_table("verified_community_record_participant")
