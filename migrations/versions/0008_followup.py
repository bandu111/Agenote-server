"""持久化受限复盘与主动跟进任务。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_followup"
down_revision = "0007_knowledge_assistance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "followup_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("decision", sa.String(20)),
        sa.Column("document_id", sa.String(160)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_followup_runs_owner_id", "followup_runs", ["owner_id"])
    op.create_index("ix_followup_runs_status", "followup_runs", ["status"])


def downgrade():
    op.drop_table("followup_runs")
