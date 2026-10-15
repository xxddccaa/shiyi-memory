"""Shared clients, confirmed operations, conversation digests and project feedback."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "e91d2_companion"
down_revision = "c82e1_agent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "todo_merge_rejections",
        sa.Column(
            "left_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("todos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "right_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("todos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.add_column(
        "episodes", sa.Column("context_revision", sa.Integer(), server_default="0", nullable=False)
    )
    for name in ("processed_sources", "total_sources"):
        op.add_column(
            "derived_artifacts", sa.Column(name, sa.Integer(), server_default="0", nullable=False)
        )
    op.add_column(
        "projects", sa.Column("revision", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column("todos", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column(
        "todos", sa.Column("project_id", pg.UUID(as_uuid=True), sa.ForeignKey("projects.id"))
    )
    op.alter_column("todos", "episode_id", nullable=True)
    op.add_column("chat_messages", sa.Column("request_id", pg.UUID(as_uuid=True)))
    op.create_unique_constraint(
        "uq_chat_request", "chat_messages", ["conversation_id", "request_id"]
    )
    op.add_column(
        "work_sessions", sa.Column("category", sa.String(50), server_default="工作", nullable=False)
    )
    op.create_table(
        "client_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("reference", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_client_events_created_at", "client_events", ["created_at"])
    op.create_table(
        "operations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id", pg.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="CASCADE")
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("payload", pg.JSONB()),
        sa.Column("preview", pg.JSONB()),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", pg.JSONB()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_operations_conversation_id", "operations", ["conversation_id"])
    op.create_index("ix_operations_expires_at", "operations", ["expires_at"])
    op.create_table(
        "conversation_digests",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("partial_message_id", pg.UUID(as_uuid=True)),
        sa.Column("partial_offset", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("content", sa.Text()),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "project_aliases",
        sa.Column("name", sa.String(200), primary_key=True),
        sa.Column(
            "project_id", pg.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
    )
    op.create_index("ix_project_aliases_project_id", "project_aliases", ["project_id"])
    op.create_table(
        "feedback",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("object_kind", sa.String(20), nullable=False),
        sa.Column("object_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("value", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("object_kind", "object_id", "revision"),
    )
    op.create_index("ix_feedback_object_id", "feedback", ["object_id"])


def downgrade():
    op.drop_table("todo_merge_rejections")
    op.drop_column("episodes", "context_revision")
    for name in ("processed_sources", "total_sources"):
        op.drop_column("derived_artifacts", name)
    for table in (
        "feedback",
        "project_aliases",
        "conversation_digests",
        "operations",
        "client_events",
    ):
        op.drop_table(table)
    op.drop_column("work_sessions", "category")
    op.drop_constraint("uq_chat_request", "chat_messages", type_="unique")
    op.drop_column("chat_messages", "request_id")
    # A downgrade cannot represent user-created todos without an episode. Refuse data loss.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM todos WHERE episode_id IS NULL")):
        raise RuntimeError(
            "Cannot downgrade while standalone todos exist; retain the additive migration"
        )
    op.alter_column("todos", "episode_id", nullable=False)
    op.drop_column("todos", "project_id")
    op.drop_column("todos", "revision")
    op.drop_column("projects", "revision")
