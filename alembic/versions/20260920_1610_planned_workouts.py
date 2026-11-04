"""Record what the service wrote into Garmin, and why.

It bounds what may be overwritten — only what this wrote, never a workout he built
himself — carries the digest that makes an unchanged rerun free, and keeps the reason
behind every weight so "why 47,5" is answerable from the database weeks later.

Revision ID: acdbc2bb690e
Revises: b9b78d55c17e
Create Date: 2026-09-20 16:10:39.249827
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'acdbc2bb690e'
down_revision: str | None = 'b9b78d55c17e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('planned_workouts',
    sa.Column('local_date', sa.String(length=10), nullable=False),
    sa.Column('program_day_id', sa.String(length=36), nullable=True),
    sa.Column('garmin_workout_id', sa.String(length=32), nullable=True),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('WRITTEN', 'FAILED', name='planstatus'), nullable=False),
    sa.Column('note', sa.String(length=250), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['principals.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['program_day_id'], ['program_days.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'local_date', name='uq_planned_date')
    )
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_planned_workouts_owner_id'), ['owner_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_planned_workouts_owner_id'))

    op.drop_table('planned_workouts')
