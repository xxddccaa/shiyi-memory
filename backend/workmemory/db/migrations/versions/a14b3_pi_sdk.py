"""Full Pi sessions, native workspaces and execution journal."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "a14b3_pi_sdk"
down_revision = "f03a2_mcp"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_workspaces",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extensions", pg.JSONB(), nullable=False, server_default="[]"))
    op.add_column("conversations", sa.Column("workspace_id", pg.UUID(), sa.ForeignKey("agent_workspaces.id", ondelete="SET NULL")))
    op.add_column("conversations", sa.Column("coding_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("conversations", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table("pi_sessions",
        sa.Column("id", pg.UUID(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", pg.JSONB()),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ready"),
        sa.Column("context", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("agent_tool_calls",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("run_id", pg.UUID(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", pg.UUID(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("arguments", pg.JSONB()), sa.Column("result", pg.JSONB()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("side_effect", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "call_id"))
    for column in ("run_id", "conversation_id"):
        op.create_index("ix_agent_tool_calls_" + column, "agent_tool_calls", [column])
    op.create_table("agent_tool_output",
        sa.Column("call_id", pg.UUID(), sa.ForeignKey("agent_tool_calls.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False))


def downgrade():
    op.drop_table("agent_tool_output")
    op.drop_table("agent_tool_calls")
    op.drop_table("pi_sessions")
    for column in ("revision", "coding_enabled", "workspace_id"):
        op.drop_column("conversations", column)
    op.drop_table("agent_workspaces")
