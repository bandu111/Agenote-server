"""账户资料与乐观版本；不改变登录邮箱。"""
from alembic import op
import sqlalchemy as sa
revision = '0012_account_profile'
down_revision = '0011_controlled_tools'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('profile', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('users', sa.Column('profile_version', sa.Integer(), nullable=False, server_default='1'))


def downgrade():
    op.drop_column('users', 'profile_version')
    op.drop_column('users', 'profile')
