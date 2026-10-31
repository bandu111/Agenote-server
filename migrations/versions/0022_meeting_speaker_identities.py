"""Persist account-scoped meeting identities independently of transcript sync."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0022_meeting_speaker_identities'
down_revision = '0021_merge_voiceprints_calendar'
branch_labels = depends_on = None


def upgrade():
    op.add_column('meetings', sa.Column('speaker_identity_revision', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('meetings', sa.Column('speaker_identities', JSONB(), nullable=False, server_default='{}'))
    op.add_column('meeting_archives', sa.Column('speaker_identity_revision', sa.Integer(), nullable=False, server_default='0'))


def downgrade():
    op.drop_column('meeting_archives', 'speaker_identity_revision')
    op.drop_column('meetings', 'speaker_identities')
    op.drop_column('meetings', 'speaker_identity_revision')
