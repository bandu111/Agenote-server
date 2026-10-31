"""临时且严格按会议隔离的匿名说话人续接。"""
from alembic import op
import sqlalchemy as sa

revision = "0010_speaker_context"
down_revision = "0009_push"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("meeting_speaker_contexts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("provider_context", sa.String(512), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_meeting_speaker_contexts_owner_id", "meeting_speaker_contexts", ["owner_id"])


def downgrade():
    op.drop_table("meeting_speaker_contexts")
