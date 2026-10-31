"""Keep user-deleted meeting documents out of automatic knowledge archival."""
from alembic import op
import sqlalchemy as sa

revision = '0014_deleted_meeting_knowledge'
down_revision = '0013_meeting_audio'
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        'deleted_meeting_knowledge',
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('document_id', sa.String(160), primary_key=True),
    )
    # Recover prior confirmed deletions when their bounded audit entries remain.
    # Existing documents are never marked deleted by this backfill.
    op.execute("""
        INSERT INTO deleted_meeting_knowledge (owner_id, document_id)
        SELECT DISTINCT k.owner_id, a.entry->>'documentId'
        FROM knowledge_libraries k
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(k.snapshot->'audit', '[]'::jsonb)) AS a(entry)
        WHERE a.entry->>'action' = '资料及其本地检索内容已移除'
          AND a.entry->>'documentId' LIKE 'meeting-%'
          AND length(a.entry->>'documentId') <= 160
          AND NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements(COALESCE(k.snapshot->'documents', '[]'::jsonb)) AS d(entry)
              WHERE d.entry->>'id' = a.entry->>'documentId'
          )
        ON CONFLICT DO NOTHING
    """)


def downgrade():
    op.drop_table('deleted_meeting_knowledge')
