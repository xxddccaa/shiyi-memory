"""Short-lived proactive Pi utterances; existing conversations and data are preserved."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "d91f6_proactive"
down_revision = "c83e5_scheduled_todos"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "proactive_utterances",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("topic", sa.String(20), nullable=False),
        sa.Column("preview", sa.Boolean(), nullable=False),
        sa.Column("settings_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("context", pg.JSONB()),
        sa.Column("checkpoint", pg.JSONB()),
        sa.Column("citations", pg.JSONB(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(50)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shown_at", sa.DateTime(timezone=True)),
        sa.Column(
            "message_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_proactive_utterances_status", "proactive_utterances", ["status"])
    op.create_index("ix_proactive_utterances_expires_at", "proactive_utterances", ["expires_at"])


def downgrade():
    op.drop_table("proactive_utterances")
