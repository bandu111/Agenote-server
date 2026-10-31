"""Add typed calendar authority fields without trusting legacy completions."""
from alembic import op
import sqlalchemy as sa

revision = "0020_calendar_authority"
down_revision = "0019_assistant_queries"
branch_labels = depends_on = None


def upgrade():
    op.add_column("action_runs", sa.Column("operation", sa.String(20), nullable=True))
    op.add_column(
        "action_runs",
        sa.Column("supersedes_id", sa.String(36), nullable=True),
    )
    op.add_column("action_runs", sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("action_runs", sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("action_runs", sa.Column("time_zone", sa.String(64), nullable=True))
    op.add_column("action_runs", sa.Column("agenda", sa.Text(), nullable=True))
    op.add_column(
        "action_runs",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column(
        "action_runs",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Deliberately nullable and deliberately not backfilled. A legacy completed row
    # must never become authoritative merely because this migration was deployed.
    op.add_column("action_runs", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_action_runs_supersedes_id_action_runs",
        "action_runs",
        "action_runs",
        ["supersedes_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_action_runs_calendar_operation",
        "action_runs",
        "operation IS NULL OR operation IN ('create', 'reschedule', 'cancel')",
    )
    # Existing non-tool actions are create commands. Their typed schedule is useful
    # for audit/display, while verified_at remains NULL so no reminder is recovered.
    op.execute("""
        UPDATE action_runs
        SET operation = 'create',
            starts_at = CASE
                WHEN content->>'start' ~ '^\d{4}-\d{2}-\d{2}T.*(Z|[+-]\d{2}:\d{2})$'
                THEN (content->>'start')::timestamptz ELSE NULL END,
            ends_at = CASE
                WHEN content->>'end' ~ '^\d{4}-\d{2}-\d{2}T.*(Z|[+-]\d{2}:\d{2})$'
                THEN (content->>'end')::timestamptz ELSE NULL END,
            time_zone = COALESCE(NULLIF(content->>'timeZone', ''), 'UTC'),
            agenda = NULLIF(content->>'agenda', '')
        WHERE COALESCE(content->>'kind', 'calendar') <> 'tool_plan'
    """)
    op.create_index(
        "ix_action_runs_owner_status_start",
        "action_runs",
        ["owner_id", "status", "starts_at"],
    )
    op.create_index("ix_action_runs_supersedes_id", "action_runs", ["supersedes_id"])


def downgrade():
    op.drop_index("ix_action_runs_supersedes_id", table_name="action_runs")
    op.drop_index("ix_action_runs_owner_status_start", table_name="action_runs")
    op.drop_constraint("ck_action_runs_calendar_operation", "action_runs", type_="check")
    op.drop_constraint("fk_action_runs_supersedes_id_action_runs", "action_runs", type_="foreignkey")
    for column in [
        "verified_at", "updated_at", "created_at", "agenda", "time_zone",
        "ends_at", "starts_at", "supersedes_id", "operation",
    ]:
        op.drop_column("action_runs", column)
