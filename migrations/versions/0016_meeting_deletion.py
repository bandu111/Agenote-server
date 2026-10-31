"""Retain an account-scoped deletion marker to reject stale device synchronization."""
from alembic import op
import sqlalchemy as sa

revision = '0016_meeting_deletion'
down_revision = '0015_meeting_minutes_details'
branch_labels = depends_on = None


def upgrade():
    op.add_column('meetings', sa.Column('deleted', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('meetings', 'deleted')
