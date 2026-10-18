"""add phone flags to users

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('phone_shared', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('users', sa.Column('phone_refused', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'phone_refused')
    op.drop_column('users', 'phone_shared')
