"""Persist structured meeting minutes alongside knowledge Markdown."""
from alembic import op
import sqlalchemy as sa

revision = '0015_meeting_minutes_details'
down_revision = '0014_deleted_meeting_knowledge'
branch_labels = depends_on = None


def upgrade():
    op.add_column('meeting_archives', sa.Column('details', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('meeting_archives', 'details')
