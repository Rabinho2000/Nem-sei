"""Multiple simultaneous service engagements per installation, and where their
commercial terms come from.

Two additions, both additive and both nullable:

`asset_service_contracts.service_kind` widens from the single value `om` to
`om`, `esco`, `monitoring`. The 92 real rows this table holds are all `om`
and are untouched by this migration -- widening a CHECK constraint's allowed
values changes no row. What it does change is `contracts/service.py`'s
reading and closing functions, which from this point on must scope by
`service_kind` -- see that module and `contracts/models.py` for why a
function that did not would have silently closed an O&M engagement the
moment an ESCO one was recorded for the same installation.

`asset_service_contracts.installation_id` and
`asset_billing_configs.contract_id` are both nullable FKs, both unenforced,
both filled in later by a backfill script rather than by this migration --
the same division of labour as 0031's `installations` versus its own
backfill. No row moves here.

A third change is not additive, and is the one that actually makes multiple
simultaneous engagements possible rather than merely representable:
`ex_asset_service_contracts_no_overlap` (0027) excludes on
`(asset_id, daterange)` alone, because `service_kind` had one value when it
was written and the two were equivalent. Left as-is it would refuse the very
thing this revision exists to allow -- the database itself would reject an
ESCO contract dated inside an installation's open O&M window, which is the
ordinary case, not an edge case. This was found by
`tests_v2/test_contract_scopes.py::test_recording_an_esco_engagement_does_not_close_an_open_om_one`
failing with `ExclusionViolation`, not by inspection; the application-level
`_close_open_contracts` scoping fix alone was not enough. The constraint is
dropped and recreated with `service_kind WITH =` added, using the same
`btree_gist` extension 0027 already installed.

Revision ID: 0032_contract_scopes
Revises: 0031_installations
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_contract_scopes"
down_revision = "0031_installations"
branch_labels = None
depends_on = None

SERVICE_KINDS_BEFORE = ("om",)
SERVICE_KINDS_AFTER = ("om", "esco", "monitoring")


def _values(options: tuple[str, ...]) -> str:
    return ", ".join(f"'{option}'" for option in options)


def upgrade() -> None:
    op.drop_constraint("ck_asset_service_contracts_kind", "asset_service_contracts", type_="check")
    op.create_check_constraint(
        "ck_asset_service_contracts_kind",
        "asset_service_contracts",
        f"service_kind IN ({_values(SERVICE_KINDS_AFTER)})",
    )
    op.add_column(
        "asset_service_contracts",
        sa.Column(
            "installation_id", sa.Integer(), sa.ForeignKey("installations.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    op.create_index("ix_asset_service_contracts_kind", "asset_service_contracts", ["service_kind", "asset_id"])
    op.create_index(
        "ix_asset_service_contracts_installation", "asset_service_contracts", ["installation_id"]
    )

    op.add_column(
        "asset_billing_configs",
        sa.Column(
            "contract_id",
            sa.Integer(),
            sa.ForeignKey("asset_service_contracts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_asset_billing_configs_contract", "asset_billing_configs", ["contract_id"])

    # See the module docstring: without `service_kind` in the exclusion, an
    # ESCO engagement dated inside an open O&M window -- the ordinary case --
    # would be refused by the database itself.
    op.execute("ALTER TABLE asset_service_contracts DROP CONSTRAINT ex_asset_service_contracts_no_overlap")
    op.execute(
        """
        ALTER TABLE asset_service_contracts
        ADD CONSTRAINT ex_asset_service_contracts_no_overlap
        EXCLUDE USING gist (
            asset_id WITH =,
            service_kind WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE asset_service_contracts DROP CONSTRAINT ex_asset_service_contracts_no_overlap")
    op.execute(
        """
        ALTER TABLE asset_service_contracts
        ADD CONSTRAINT ex_asset_service_contracts_no_overlap
        EXCLUDE USING gist (
            asset_id WITH =,
            daterange(valid_from, valid_to, '[)') WITH &&
        )
        """
    )

    op.drop_index("ix_asset_billing_configs_contract", table_name="asset_billing_configs")
    op.drop_column("asset_billing_configs", "contract_id")

    op.drop_index("ix_asset_service_contracts_installation", table_name="asset_service_contracts")
    op.drop_index("ix_asset_service_contracts_kind", table_name="asset_service_contracts")
    op.drop_column("asset_service_contracts", "installation_id")
    op.drop_constraint("ck_asset_service_contracts_kind", "asset_service_contracts", type_="check")
    op.create_check_constraint(
        "ck_asset_service_contracts_kind",
        "asset_service_contracts",
        f"service_kind IN ({_values(SERVICE_KINDS_BEFORE)})",
    )
