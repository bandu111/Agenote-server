"""Per-account Weixin authorization, never reuse Gateway administrator QR."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision='0018_wechat_bindings'
down_revision='0017_work_outcomes'
branch_labels=depends_on=None
def upgrade():
    op.create_table('wechat_bindings',
        sa.Column('owner_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),primary_key=True),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('status',sa.String(30),nullable=False),
        sa.Column('peer_hash',sa.String(64),unique=True,nullable=True),
        sa.Column('encrypted',sa.Text(),nullable=True),
        sa.Column('state',postgresql.JSONB(),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_wechat_bindings_status','wechat_bindings',['status'])
    op.create_index('ix_wechat_bindings_updated_at','wechat_bindings',['updated_at'])
def downgrade(): op.drop_table('wechat_bindings')
