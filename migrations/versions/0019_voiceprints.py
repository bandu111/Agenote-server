"""Account-scoped encrypted voiceprint registration and consent audit."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0019_voiceprints'
down_revision = '0018_wechat_bindings'
branch_labels = depends_on = None


def upgrade():
    op.create_table('voiceprint_profiles',
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('template', sa.LargeBinary()),
        sa.Column('model_version', sa.String(150), nullable=False),
        sa.Column('key_id', sa.String(50), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True)),
        sa.Column('device', JSONB(), nullable=False))
    op.create_table('voiceprint_enrollments',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('base_version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('samples', JSONB(), nullable=False),
        sa.Column('candidate', sa.LargeBinary()),
        sa.Column('model_version', sa.String(150), nullable=False),
        sa.Column('key_id', sa.String(50), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_voiceprint_enrollments_owner_id', 'voiceprint_enrollments', ['owner_id'])
    op.create_index('ix_voiceprint_enrollments_expires_at', 'voiceprint_enrollments', ['expires_at'])
    op.create_table('voiceprint_audits',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('consent_version', sa.String(40), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_voiceprint_audits_owner_id', 'voiceprint_audits', ['owner_id'])


def downgrade():
    for name in ('voiceprint_audits', 'voiceprint_enrollments', 'voiceprint_profiles'):
        op.drop_table(name)
