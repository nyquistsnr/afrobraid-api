"""add indexes for admin list filters and sorts

Revision ID: b9e4c2d7a1f6
Revises: a3d7e5f0c2b8
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b9e4c2d7a1f6'
down_revision: Union[str, None] = 'a3d7e5f0c2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Columns the admin dashboard filters or sorts on that previously forced a full scan.
INDEXES = [
    ('users', 'user_type'),
    ('users', 'created_at'),
    ('bookings', 'created_at'),
    ('bookings', 'ends_at'),
    ('booking_payments', 'created_at'),
    ('booking_payments', 'status'),
    ('reviews', 'status'),
    ('reviews', 'created_at'),
    ('chat_reports', 'status'),
    ('chat_reports', 'created_at'),
    ('contact_submissions', 'created_at'),
    ('admin_invites', 'created_at'),
]


def upgrade() -> None:
    for table, column in INDEXES:
        op.create_index(op.f(f'ix_{table}_{column}'), table, [column], unique=False, if_not_exists=True)


def downgrade() -> None:
    for table, column in reversed(INDEXES):
        op.drop_index(op.f(f'ix_{table}_{column}'), table_name=table, if_exists=True)
