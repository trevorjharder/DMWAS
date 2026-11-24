"""Connections, private messages, report evidence and digest delivery.

Revision ID: b08e5f6a7b8c
Revises: a07d4e5f6a7b
"""
from alembic import op
import sqlalchemy as sa


revision = "b08e5f6a7b8c"
down_revision = "a07d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('message_digest',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('through_id', sa.Integer(), nullable=False),
    sa.Column('unread_count', sa.Integer(), nullable=False),
    sa.Column('prepared_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('message_pair',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('low_id', sa.Integer(), nullable=True),
    sa.Column('high_id', sa.Integer(), nullable=True),
    sa.Column('requested_by_low', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('low_blocked', sa.Boolean(), nullable=False),
    sa.Column('high_blocked', sa.Boolean(), nullable=False),
    sa.Column('low_read_through', sa.Integer(), nullable=False),
    sa.Column('high_read_through', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('pending','accepted','declined','disconnected')", name='ck_message_pair_status'),
    sa.CheckConstraint('low_id < high_id', name='ck_message_pair_order'),
    sa.ForeignKeyConstraint(['high_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['low_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('low_id', 'high_id', name='uq_message_pair_members')
    )
    op.create_index(op.f('ix_message_pair_high_id'), 'message_pair', ['high_id'], unique=False)
    op.create_index(op.f('ix_message_pair_low_id'), 'message_pair', ['low_id'], unique=False)
    op.create_table('messaging_preference',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('digest_enabled', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('messaging_quota',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('bucket', sa.String(length=12), nullable=False),
    sa.Column('window_number', sa.BigInteger(), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'bucket')
    )
    op.create_table('message_report',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('reporter_id', sa.Integer(), nullable=True),
    sa.Column('subject_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=24), nullable=False),
    sa.Column('note', sa.String(length=800), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('open','resolved','dismissed')", name='ck_message_report_status'),
    sa.ForeignKeyConstraint(['pair_id'], ['message_pair.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reporter_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['subject_id'], ['user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_message_report_pair_id'), 'message_report', ['pair_id'], unique=False)
    op.create_index(op.f('ix_message_report_reporter_id'), 'message_report', ['reporter_id'], unique=False)
    op.create_index('ix_message_report_status_id', 'message_report', ['status', 'id'], unique=False)
    op.create_index(op.f('ix_message_report_subject_id'), 'message_report', ['subject_id'], unique=False)
    op.create_table('private_message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('pair_id', sa.Integer(), nullable=False),
    sa.Column('sent_by_low', sa.Boolean(), nullable=False),
    sa.Column('body', sa.String(length=2000), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['pair_id'], ['message_pair.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    )
    op.create_index(op.f('ix_private_message_created_at'), 'private_message', ['created_at'], unique=False)
    op.create_index('ix_private_message_pair_id_id', 'private_message', ['pair_id', 'id'], unique=False)
    op.create_table('message_evidence',
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('original_message_id', sa.Integer(), nullable=False),
    sa.Column('from_reporter', sa.Boolean(), nullable=False),
    sa.Column('body', sa.String(length=2000), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['message_report.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('report_id', 'original_message_id')
    )
    op.create_table('message_report_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=12), nullable=False),
    sa.Column('note', sa.String(length=800), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['report_id'], ['message_report.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_message_report_event_actor_id'), 'message_report_event', ['actor_id'], unique=False)
    op.create_index(op.f('ix_message_report_event_report_id'), 'message_report_event', ['report_id'], unique=False)

def downgrade():
    op.drop_index(op.f('ix_message_report_event_report_id'), table_name='message_report_event')
    op.drop_index(op.f('ix_message_report_event_actor_id'), table_name='message_report_event')
    op.drop_table('message_report_event')
    op.drop_table('message_evidence')
    op.drop_index('ix_private_message_pair_id_id', table_name='private_message')
    op.drop_index(op.f('ix_private_message_created_at'), table_name='private_message')
    op.drop_table('private_message')
    op.drop_index(op.f('ix_message_report_subject_id'), table_name='message_report')
    op.drop_index('ix_message_report_status_id', table_name='message_report')
    op.drop_index(op.f('ix_message_report_reporter_id'), table_name='message_report')
    op.drop_index(op.f('ix_message_report_pair_id'), table_name='message_report')
    op.drop_table('message_report')
    op.drop_table('messaging_quota')
    op.drop_table('messaging_preference')
    op.drop_index(op.f('ix_message_pair_low_id'), table_name='message_pair')
    op.drop_index(op.f('ix_message_pair_high_id'), table_name='message_pair')
    op.drop_table('message_pair')
    op.drop_table('message_digest')
