"""ingredient aliases

The personal dictionary: how the owner names a product in a recipe → a catalogue product.

The revision number was restored by hand: the file was deleted by mistake after the
migration had already been applied on the server. The number must not change.

Revision ID: 3eb1256d2571
Revises: fa340a0d2e23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "3eb1256d2571"
down_revision: str | None = "fa340a0d2e23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingredient_aliases",
        sa.Column("raw_name", sa.String(length=250), nullable=False),
        sa.Column("canonical_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=80), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_id"], ["canonical_products.id"],
            name="fk_ingredient_alias_product", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["principals.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "raw_name", name="uq_ingredient_alias"),
    )
    op.create_index(
        op.f("ix_ingredient_aliases_canonical_id"), "ingredient_aliases",
        ["canonical_id"], unique=False,
    )
    op.create_index(
        op.f("ix_ingredient_aliases_owner_id"), "ingredient_aliases",
        ["owner_id"], unique=False,
    )
    op.create_index(
        op.f("ix_ingredient_aliases_raw_name"), "ingredient_aliases",
        ["raw_name"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ingredient_aliases_raw_name"), table_name="ingredient_aliases")
    op.drop_index(op.f("ix_ingredient_aliases_owner_id"), table_name="ingredient_aliases")
    op.drop_index(op.f("ix_ingredient_aliases_canonical_id"), table_name="ingredient_aliases")
    op.drop_table("ingredient_aliases")
