"""Track manual work-session edits independently from episode source versions."""
import sqlalchemy as sa
from alembic import op

revision = "b72d4_session_edit"
down_revision = "a14b3_pi_sdk"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("work_sessions", sa.Column("edit_revision", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    op.drop_column("work_sessions", "edit_revision")
