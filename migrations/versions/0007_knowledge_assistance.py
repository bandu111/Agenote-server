"""文档分析队列及版本绑定的用户决定回执。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_knowledge_assistance"
down_revision = "0006_actions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_analyses",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", sa.String(160), nullable=False),
        sa.Column("version_id", sa.String(160), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_document_analyses_owner_id", "document_analyses", ["owner_id"])
    op.create_index("ix_document_analyses_status", "document_analyses", ["status"])
    op.create_table(
        "assistance_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_assistance_receipts_owner_id", "assistance_receipts", ["owner_id"]
    )


def downgrade():
    op.drop_table("assistance_receipts")
    op.drop_table("document_analyses")
