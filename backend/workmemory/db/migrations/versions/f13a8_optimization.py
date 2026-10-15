"""Independent confirmed memories and durable local maintenance."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f13a8_optimization"
down_revision = "e02f7_evidence_policy"
branch_labels = depends_on = None


def upgrade():
    for column in ("conversation_id", "message_id"):
        op.alter_column("memory_candidates", column, nullable=True)
    op.add_column("memory_candidates", sa.Column("origin", JSONB(), nullable=False, server_default="{}"))
    op.add_column("memory_candidates", sa.Column("source_episode_id", UUID()))
    op.create_index("ix_memory_candidates_source_episode_id", "memory_candidates", ["source_episode_id"])
    op.alter_column("operations", "conversation_id", nullable=True)
    op.add_column("operations", sa.Column("origin", JSONB(), nullable=False, server_default="{}"))
    op.add_column("operations", sa.Column("request_id", UUID(), unique=True))
    op.add_column("proactive_utterances", sa.Column("trigger", sa.String(30), nullable=False, server_default="timer"))
    op.add_column("proactive_utterances", sa.Column("trigger_id", UUID(), unique=True))
    op.create_table("maintenance_tasks",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", JSONB()), sa.Column("error", sa.String(80)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("capture_incidents",
        sa.Column("id", UUID(), primary_key=True), sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)), sa.Column("details", JSONB(), nullable=False))


def downgrade():
    raise RuntimeError("This migration protects retained history and independent memories; restore a verified backup.")
