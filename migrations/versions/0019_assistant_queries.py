"""Allow account-scoped assistant queries without a meeting."""
from alembic import op
import sqlalchemy as sa

revision = "0019_assistant_queries"
down_revision = "0018_wechat_bindings"
branch_labels = depends_on = None


def upgrade():
    op.alter_column("outcome_runs", "meeting_id", existing_type=sa.String(length=36), nullable=True)


def downgrade():
    op.alter_column("outcome_runs", "meeting_id", existing_type=sa.String(length=36), nullable=False)
