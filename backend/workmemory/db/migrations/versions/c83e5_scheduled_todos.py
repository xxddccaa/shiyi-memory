"""Explicit schedules and durable reminder occurrences; no business data removal."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c83e5_scheduled_todos"
down_revision = "b72d4_session_edit"
branch_labels = None
depends_on = None


def upgrade():
    for column in [
        sa.Column("origin", sa.String(20), nullable=False, server_default="legacy"),
        sa.Column("timezone", sa.String(80), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("repeat", sa.String(20), nullable=False, server_default="once"),
        sa.Column("weekdays", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("local_time", sa.String(8), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notify_pet", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("schedule_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
    ]:
        op.add_column("todos", column)
    op.create_index("ix_todos_due_at", "todos", ["due_at"])
    for column in [
        sa.Column("schedule_revision", sa.Integer()),
        sa.Column("planned_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("shown_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="SET NULL")),
    ]:
        op.add_column("reminders", column)
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])


def downgrade():
    op.drop_index("ix_reminders_due_at", "reminders")
    for key in ["message_id", "missed_count", "shown_revision", "revision", "status", "planned_at", "schedule_revision"]:
        op.drop_column("reminders", key)
    op.drop_index("ix_todos_due_at", "todos")
    for key in ["conversation_id", "schedule_revision", "notify_pet", "paused", "local_time", "weekdays", "repeat", "timezone", "origin"]:
        op.drop_column("todos", key)
