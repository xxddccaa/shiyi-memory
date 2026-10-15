"""Business capabilities and MCP evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "f03a2_mcp"
down_revision = "e91d2_companion"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column("context", pg.JSONB(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("external_citations", pg.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "todo_tombstones",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", pg.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")
        ),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("source_ids", pg.JSONB(), nullable=False),
        sa.Column(
            "deleted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for column in ["project_id", "text_hash"]:
        op.create_index("ix_todo_tombstones_" + column, "todo_tombstones", [column])
    op.create_table(
        "external_sources",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column(
            "retrieved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ["run_id", "conversation_id", "expires_at"]:
        op.create_index("ix_external_sources_" + column, "external_sources", [column])


def downgrade():
    op.drop_table("external_sources")
    op.drop_table("todo_tombstones")
    op.drop_column("chat_messages", "external_citations")
    op.drop_column("chat_messages", "context")
