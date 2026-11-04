"""Recognise a session by the template it ran, not by its date.

He shifts a day — Saturday's stair session ran on a Friday — and the activity name comes
back truncated, so neither the date nor the title identifies what kind of session it was.
Garmin puts the executed template's id on the activity; that does.

`program_days.garmin_workout_id` is the other half: one durable template per weekday,
rewritten in place, so nothing is ever deleted and calendar entries stay attached.

Revision ID: b9b78d55c17e
Revises: 61d2e7f9c547
Create Date: 2026-09-20 16:07:24.850593
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b9b78d55c17e'
down_revision: str | None = '61d2e7f9c547'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('program_days', schema=None) as batch_op:
        batch_op.add_column(sa.Column('garmin_workout_id', sa.String(length=32), nullable=True))
        # Existing rows are all strength days; the default is dropped afterwards so the
        # column stays explicit for anything written from now on.
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False,
                                      server_default='strength'))

    with op.batch_alter_table('program_days', schema=None) as batch_op:
        batch_op.alter_column('kind', server_default=None)

    with op.batch_alter_table('workout_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('garmin_workout_id', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_workout_sessions_garmin_workout_id'), ['garmin_workout_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('workout_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workout_sessions_garmin_workout_id'))
        batch_op.drop_column('garmin_workout_id')

    with op.batch_alter_table('program_days', schema=None) as batch_op:
        batch_op.drop_column('kind')
        batch_op.drop_column('garmin_workout_id')

