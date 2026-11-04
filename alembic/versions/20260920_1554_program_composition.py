"""What a program day is made of, and how each exercise gets harder.

Until now the database knew a day's title and nothing else: the exercise detail lived in
Garmin. That made the plan derivable only from what was logged, so an exercise skipped
once left the programme silently. It also left the progression rule without a target to
compare against.

`cue` on the exercise is the technique note that reaches the watch on the first set.

Revision ID: 61d2e7f9c547
Revises: 6d497bb7ea10
Create Date: 2026-09-20 15:54:34.069067
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '61d2e7f9c547'
down_revision: str | None = '6d497bb7ea10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('program_exercises',
    sa.Column('program_day_id', sa.String(length=36), nullable=False),
    sa.Column('exercise_id', sa.String(length=36), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('sets', sa.Integer(), nullable=False),
    sa.Column('target_min', sa.Integer(), nullable=False),
    sa.Column('target_max', sa.Integer(), nullable=False),
    sa.Column('rest_seconds', sa.Integer(), nullable=False),
    sa.Column('progression', sa.Enum('LOAD', 'ASSIST', 'REPS', 'TIME', name='progression'), nullable=False),
    sa.Column('seed_value', sa.Float(), nullable=True),
    sa.Column('note', sa.String(length=250), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['exercise_id'], ['exercises.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_id'], ['principals.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['program_day_id'], ['program_days.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'program_day_id', 'exercise_id', name='uq_program_exercise')
    )
    with op.batch_alter_table('program_exercises', schema=None) as batch_op:
        batch_op.create_index('ix_program_exercise_day', ['owner_id', 'program_day_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_program_exercises_owner_id'), ['owner_id'], unique=False)

    with op.batch_alter_table('exercises', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cue', sa.Text(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('exercises', schema=None) as batch_op:
        batch_op.drop_column('cue')

    with op.batch_alter_table('program_exercises', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_program_exercises_owner_id'))
        batch_op.drop_index('ix_program_exercise_day')

    op.drop_table('program_exercises')
