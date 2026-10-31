"""内部任务和幂等工具回执。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = '0011_controlled_tools'
down_revision = '0010_speaker_context'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('internal_tasks', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('meeting_id', sa.String(36), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('content', postgresql.JSONB(), nullable=False))
    op.create_index('ix_internal_tasks_owner_id', 'internal_tasks', ['owner_id'])
    op.create_table('tool_receipts', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('plan_id', sa.String(36), sa.ForeignKey('action_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False), sa.Column('content', postgresql.JSONB(), nullable=False))
    op.create_index('ix_tool_receipts_owner_id', 'tool_receipts', ['owner_id'])


def downgrade():
    op.drop_table('tool_receipts')
    op.drop_table('internal_tasks')
