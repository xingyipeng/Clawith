"""Add multi-channel support: composite unique on (agent_id, channel_type), add discord enum.

Revision ID: add_multi_channel
Revises: add_quota_fields
Create Date: 2026-03-05
"""
from alembic import op
import sqlalchemy as sa

revision = "add_multi_channel"
down_revision = "add_quota_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old unique constraint on agent_id alone
    op.drop_constraint("channel_configs_agent_id_key", "channel_configs", type_="unique")

    # 2. Add discord to the channel_type enum
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'discord'")

    # 3. Add composite unique constraint (agent_id, channel_type)
    op.create_unique_constraint(
        "uq_channel_configs_agent_channel",
        "channel_configs",
        ["agent_id", "channel_type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_channel_configs_agent_channel", "channel_configs", type_="unique")
    op.create_unique_constraint("channel_configs_agent_id_key", "channel_configs", ["agent_id"])
