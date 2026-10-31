"""知识快照及可追溯检索节点，按账户级联清理。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_knowledge"
down_revision = "0004_progress_items"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_libraries",
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
    )
    op.create_table(
        "knowledge_nodes",
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
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("node", JSONB(), nullable=False),
    )
    op.create_index("ix_knowledge_nodes_owner_id", "knowledge_nodes", ["owner_id"])
    op.create_index(
        "ix_knowledge_nodes_document_id", "knowledge_nodes", ["document_id"]
    )
    op.create_table(
        "knowledge_index_states",
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
        sa.Column("status", sa.String(40), nullable=False),
        sa.UniqueConstraint(
            "owner_id", "document_id", "version_id", name="uq_knowledge_index_version"
        ),
    )
    op.create_index(
        "ix_knowledge_index_states_owner_id", "knowledge_index_states", ["owner_id"]
    )


def downgrade():
    for table in ["knowledge_index_states", "knowledge_nodes", "knowledge_libraries"]:
        op.drop_table(table)
