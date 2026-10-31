"""为会议纪要建立独立的私有归档记录。"""

from alembic import op
import sqlalchemy as sa

revision = "0003_meeting_archives"
down_revision = "0002_meetings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meeting_archives",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column(
            "meeting_id",
            sa.String(36),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("meeting_id", name="uq_meeting_archive_meeting"),
    )
    op.create_index("ix_meeting_archives_owner_id", "meeting_archives", ["owner_id"])


def downgrade():
    op.drop_table("meeting_archives")
