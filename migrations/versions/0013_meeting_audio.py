"""账户隔离音频分块，不将长会议音频塞入知识库快照。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = '0013_meeting_audio'
down_revision = '0012_account_profile'
branch_labels = depends_on = None


def upgrade():
    op.create_table('meeting_audio',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('meeting_id', sa.String(36), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('offset', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('metadata_json', postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint('meeting_id', 'sha256', name='uq_meeting_audio_hash'))
    op.create_index('ix_meeting_audio_owner_id', 'meeting_audio', ['owner_id'])
    op.create_index('ix_meeting_audio_meeting_id', 'meeting_audio', ['meeting_id'])
    op.create_table('meeting_audio_chunks',
        sa.Column('audio_id', sa.String(36), sa.ForeignKey('meeting_audio.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('offset', sa.BigInteger(), primary_key=True),
        sa.Column('data', sa.LargeBinary(), nullable=False))


def downgrade():
    op.drop_table('meeting_audio_chunks')
    op.drop_table('meeting_audio')
