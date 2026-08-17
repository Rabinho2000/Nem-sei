"""Harden foundation job cancellation and audit immutability.

Revision ID: 0002_job_hardening
Revises: 0001_v2_foundation
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_job_hardening"
down_revision = "0001_v2_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)))
    op.execute("""CREATE FUNCTION job_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'job_events are append-only'; END; $$""")
    op.execute("CREATE TRIGGER job_events_no_update BEFORE UPDATE ON job_events FOR EACH ROW EXECUTE FUNCTION job_events_immutable()")
    op.execute("CREATE TRIGGER job_events_no_delete BEFORE DELETE ON job_events FOR EACH ROW EXECUTE FUNCTION job_events_immutable()")


def downgrade() -> None:
    op.execute("DROP TRIGGER job_events_no_delete ON job_events")
    op.execute("DROP TRIGGER job_events_no_update ON job_events")
    op.execute("DROP FUNCTION job_events_immutable()")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("cancellation_requested_at")
