"""Keep the uploaded workbook itself, not just a path to it.

`report_source_files` recorded a `stored_path`, but the web container has no
writable storage at all -- its only mount is the read-only database-URL secret
-- so a financial model uploaded through the browser had nowhere to live and
its path would have pointed at a file that vanished with the next deploy.

The bytes go in the database instead. That needs no compose change (the
alternative was a new volume plus a container recreate, on a compose file that
carries another session's pending cutover), it is covered by the existing
`pg_dump -Fc` backup, and it survives `up -d --build`. The trade-off, taken
deliberately: the real workbooks are ~16.5 MB each, so the dump stops being
small in proportion to how many are uploaded.

Nullable, because a row may legitimately describe a file kept elsewhere -- and
because rows written before this column existed cannot invent one. There are
zero such rows today.

Revision ID: 0022_source_file_content
Revises: 0021_asset_audit_actions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_source_file_content"
down_revision = "0021_asset_audit_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("report_source_files", sa.Column("content", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("report_source_files", "content")
