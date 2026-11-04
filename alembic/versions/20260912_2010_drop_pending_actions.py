"""drop pending actions

Web confirmations are gone: they made the owner confirm his own words. He would say
"I weighed it, it is 217", and the system asked him to follow a link and press a
button while showing the old weight the whole time.

The pantry's guard comes from elsewhere, and that stays: nothing is deleted, everything
is a movement; a database copy every 15 minutes; the agent acts only when told to.

Revision ID: 8c41d90ae7b2
Revises: 3eb1256d2571
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "8c41d90ae7b2"
down_revision: str | None = "3eb1256d2571"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_moves", schema=None) as batch_op:
        batch_op.drop_column("pending_action_id")
    op.drop_index(op.f("ix_pending_actions_owner_id"), table_name="pending_actions")
    op.drop_table("pending_actions")


def downgrade() -> None:
    """There is no way back: the mechanism was removed deliberately, not temporarily."""
    raise NotImplementedError("Page confirmations are not coming back")
