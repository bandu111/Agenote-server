"""持久行动状态，图检查点由官方独立安装脚本管理。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_actions"
down_revision = "0005_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "action_runs",
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
            nullable=False,
        ),
        sa.Column("intent_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.UniqueConstraint("owner_id", "intent_key", name="uq_action_owner_intent"),
    )
    op.create_index("ix_action_runs_owner_id", "action_runs", ["owner_id"])


def downgrade():
    op.drop_table("action_runs")
