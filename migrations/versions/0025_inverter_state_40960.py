"""Reclassify the imported readings whose only state code was 40960.

`availability_status` on `device_status_facts` is a *derived* column: the
provider's own evidence is `metadata_json->>'v1_inverter_state'`, which this
migration does not touch. When the classification in `diagnostics/rules.py`
learns a code, the rows already imported under the old answer have to move
too -- otherwise the fleet keeps reading "desconhecido" until every device is
re-imported, which for V1 history will never happen.

Scoped as narrowly as the claim: only rows that are still `unknown` *and*
carry exactly `40960`. A row someone has since corrected by hand, or one whose
status came from anywhere but that code, is left alone.

Revision ID: 0025_inverter_state_40960
Revises: 0024_automation_audit_actions
"""
from __future__ import annotations

from alembic import op

revision = "0025_inverter_state_40960"
down_revision = "0024_automation_audit_actions"
branch_labels = None
depends_on = None

STATE = "40960"


def upgrade() -> None:
    op.execute(
        "UPDATE device_status_facts SET availability_status = 'standby'"
        f" WHERE availability_status = 'unknown' AND metadata_json ->> 'v1_inverter_state' = '{STATE}'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE device_status_facts SET availability_status = 'unknown'"
        f" WHERE availability_status = 'standby' AND metadata_json ->> 'v1_inverter_state' = '{STATE}'"
    )
