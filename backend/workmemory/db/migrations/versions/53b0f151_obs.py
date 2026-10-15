"""Interaction observations and reversible OCR differences.

Revision ID: 53b0f151_obs
Revises: 290a33eef282
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "53b0f151_obs"
down_revision = "290a33eef282"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "observations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("episode_id", sa.UUID(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("activity_id", sa.UUID(), sa.ForeignKey("activities.id")),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("app", sa.String(200), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("window_id", sa.String(200), nullable=False),
        sa.Column("interaction_id", sa.UUID()),
        sa.Column("trigger", sa.String(30)),
        sa.Column("phase", sa.String(20)),
        sa.Column("event_at", sa.DateTime(timezone=True)),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("delta", postgresql.JSONB()),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("meaningful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("capture_meta", postgresql.JSONB(), nullable=False),
    )
    for name in ("episode_id", "activity_id", "interaction_id"):
        op.create_index(f"ix_observations_{name}", "observations", [name])
    op.create_index(
        "ix_observations_scope", "observations", ["episode_id", "app", "window_id", "captured_at"]
    )


def downgrade():
    op.drop_table("observations")
