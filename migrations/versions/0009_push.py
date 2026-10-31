"""系统推送设备绑定与持久化去重投递。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_push"
down_revision = "0008_followup"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("push_devices",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("installation_hash", sa.String(64), nullable=False),
        sa.Column("registration_id", sa.String(200), nullable=False),
        sa.Column("app_key", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(10), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_push_devices_owner_id", "push_devices", ["owner_id"])
    op.create_table("push_deliveries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_push_deliveries_owner_id", "push_deliveries", ["owner_id"])
    op.create_index("ix_push_deliveries_status", "push_deliveries", ["status"])


def downgrade():
    op.drop_table("push_deliveries")
    op.drop_table("push_devices")
