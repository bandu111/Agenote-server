"""账户隔离的进展实体及人工变更记录。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_progress_items"
down_revision = "0003_meeting_archives"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "progress_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "meeting_id",
            sa.String(36),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_key", sa.String(180), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("overrides", postgresql.JSONB(), nullable=False),
        sa.Column("history", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "source_key", name="uq_progress_owner_source"),
    )
    op.create_index("ix_progress_items_owner_id", "progress_items", ["owner_id"])
    op.create_index("ix_progress_items_meeting_id", "progress_items", ["meeting_id"])


def downgrade():
    op.drop_table("progress_items")
