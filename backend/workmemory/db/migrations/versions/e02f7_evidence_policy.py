"""Quarantine legacy automatic summaries; preserve bodies for human review."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "e02f7_evidence_policy"
down_revision = "d91f6_proactive"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("episodes", sa.Column("evidence_state", sa.String(24), nullable=False, server_default="pending"))
    op.add_column("episodes", sa.Column("evidence_policy", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("episodes", sa.Column("evidence_expires_at", sa.DateTime(timezone=True)))
    op.add_column("episodes", sa.Column("evidence_reviewed_revision", sa.Integer(), nullable=False, server_default="0"))
    # Old edit_revision conflated title/category edits; keep the stored title for
    # human review without treating a category-only edit as title confirmation.
    op.add_column("work_sessions", sa.Column("title_confirmed", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("work_facts", sa.Column("provenance", pg.JSONB(), nullable=False, server_default="{}"))
    op.create_table("evidence_archives",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("object_kind", sa.String(20), nullable=False),
        sa.Column("object_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", pg.JSONB(none_as_null=True)),
        sa.Column("sources", pg.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("object_kind", "object_id", "revision"))
    op.create_index("ix_evidence_archives_object_id", "evidence_archives", ["object_id"])
    op.create_index("ix_evidence_archives_expires_at", "evidence_archives", ["expires_at"])
    op.execute("""UPDATE episodes SET evidence_state = CASE
        WHEN deleted_at IS NOT NULL THEN 'expired'
        WHEN corrected THEN 'user_confirmed'
        WHEN summary IS NOT NULL THEN 'legacy_unverified'
        WHEN status = 'expired' THEN 'expired' ELSE 'pending' END,
        evidence_policy = CASE WHEN corrected THEN 1 ELSE 0 END""")
    # The startup transaction archives display data before clearing active consumers.
    # The eligibility gate is already effective before that transaction starts.
    op.execute("""INSERT INTO app_state(key,value) VALUES
        ('evidence_policy', jsonb_build_object('initialized', false)) ON CONFLICT(key) DO NOTHING""")


def downgrade():
    raise RuntimeError("Evidence policy protects retained history; restore a verified backup instead of downgrading.")
