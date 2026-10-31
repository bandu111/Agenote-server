"""Private durable role-aware reports, isolated from executable business actions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision='0017_work_outcomes'
down_revision='0016_meeting_deletion'
branch_labels=depends_on=None

def upgrade():
    op.create_table('outcome_runs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('owner_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
        sa.Column('meeting_id',sa.String(36),sa.ForeignKey('meetings.id',ondelete='CASCADE'),nullable=False),
        sa.Column('intent_key',sa.String(64),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('status',sa.String(40),nullable=False),
        sa.Column('content',postgresql.JSONB(),nullable=False),
        sa.UniqueConstraint('owner_id','intent_key',name='uq_outcome_owner_intent'))
    for name in ['owner_id','meeting_id','status']:
        op.create_index('ix_outcome_runs_'+name,'outcome_runs',[name])

def downgrade():
    op.drop_table('outcome_runs')
