"""The monthly workflow: validate coverage, generate, review, approve.

`PortfolioDataset` already aggregates a period; this migration adds the layer
on top that turns "the numbers for July" into an actual operational decision —
portfolio, period, coverage checked, individual reports generated, reviewed,
approved. Nothing here computes a metric. `portfolio_report_runs` points at the
`PortfolioDataset` that was current when it was generated, and
`portfolio_report_run_members` points at the individual `ReportSnapshot` each
ready member actually got, so both layers stay a single source rather than a
second copy of the numbers.

A run has no "draft" state stored: validating coverage is a read against the
data that already exists (build the dataset, look at its coverage_json), and a
run only starts existing once an operator actually generates it. From there it
is `generated` -> `reviewed` -> `approved`, and once `approved` a trigger
refuses to touch the run or its members again — the same append-only guarantee
`report_snapshots` and `portfolio_snapshots` already give the records beneath
it. No distribution happens from this layer; that is deliberately a later
milestone, and this is the state a scheduler will eventually read to know
whether to run it at all.

Revision ID: 0014_portfolio_report_runs
Revises: 0013_portfolios
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_portfolio_report_runs"
down_revision = "0013_portfolios"
branch_labels = None
depends_on = None


RUN_STATUSES = ("generated", "reviewed", "approved")
MEMBER_STATUSES = ("ready", "blocked")


def upgrade() -> None:
    op.create_table(
        "portfolio_report_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        # The aggregate as of generation. Re-generating before approval points
        # this at a fresh PortfolioDataset; it is not rebuilt in place.
        sa.Column("portfolio_dataset_id", sa.Integer(), nullable=False),
        # A copy of the dataset's coverage at generation time, so the run's own
        # history is readable even though PortfolioDataset itself is a cheap,
        # rebuildable artefact and not literally frozen.
        sa.Column("coverage_json", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_by", sa.String(length=120), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by", sa.String(length=120)),
        sa.Column("review_notes", sa.Text()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["portfolio_dataset_id"], ["portfolio_datasets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"status IN {RUN_STATUSES!r}", name="ck_portfolio_report_runs_status"),
        sa.CheckConstraint("period_end > period_start", name="ck_portfolio_report_runs_period"),
        sa.CheckConstraint(
            "status = 'generated' OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)",
            name="ck_portfolio_report_runs_reviewed",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)",
            name="ck_portfolio_report_runs_approved",
        ),
        sa.CheckConstraint(
            "status <> 'generated' OR (reviewed_at IS NULL AND approved_at IS NULL)",
            name="ck_portfolio_report_runs_generated_is_untouched",
        ),
        sa.UniqueConstraint("portfolio_id", "period_start", "period_end", name="uq_portfolio_report_runs_period"),
    )
    op.create_index("ix_portfolio_report_runs_portfolio", "portfolio_report_runs", ["portfolio_id", "period_start"])

    op.create_table(
        "portfolio_report_run_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text()),
        # The individual report this member actually got. Filled in the same
        # transaction that marks the member ready, through the same
        # assemble_asset_report + snapshot_dataset path an individual report
        # uses on its own — never a second calculation.
        sa.Column("report_snapshot_id", sa.Integer()),
        sa.ForeignKeyConstraint(["run_id"], ["portfolio_report_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["report_snapshot_id"], ["report_snapshots.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"status IN {MEMBER_STATUSES!r}", name="ck_portfolio_report_run_members_status"),
        sa.CheckConstraint(
            "(status = 'ready') = (report_snapshot_id IS NOT NULL)",
            name="ck_portfolio_report_run_members_snapshot",
        ),
        sa.CheckConstraint(
            "status = 'ready' OR reason IS NOT NULL",
            name="ck_portfolio_report_run_members_reason",
        ),
        sa.UniqueConstraint("run_id", "asset_id", name="uq_portfolio_report_run_members_asset"),
    )
    op.create_index("ix_portfolio_report_run_members_run", "portfolio_report_run_members", ["run_id"])

    # Once approved, a run is the record of a decision that was made. Neither
    # the run nor which members it covered may change after that, mirroring
    # the append-only guarantee already given to report_snapshots and
    # portfolio_snapshots.
    op.execute(
        """
        CREATE FUNCTION portfolio_report_runs_locked() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'approved' THEN
                    RAISE EXCEPTION 'an approved portfolio report run cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.status = 'approved' THEN
                RAISE EXCEPTION 'an approved portfolio report run cannot be changed';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER portfolio_report_runs_lock BEFORE UPDATE OR DELETE ON portfolio_report_runs"
        " FOR EACH ROW EXECUTE FUNCTION portfolio_report_runs_locked()"
    )
    op.execute(
        """
        CREATE FUNCTION portfolio_report_run_members_locked() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            run_status text;
        BEGIN
            SELECT status INTO run_status FROM portfolio_report_runs WHERE id = OLD.run_id;
            IF run_status = 'approved' THEN
                RAISE EXCEPTION 'a member of an approved portfolio report run cannot be changed';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER portfolio_report_run_members_lock BEFORE UPDATE OR DELETE ON portfolio_report_run_members"
        " FOR EACH ROW EXECUTE FUNCTION portfolio_report_run_members_locked()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT count(*) FROM portfolio_report_runs")).scalar_one()
    if existing:
        raise RuntimeError(
            f"Refusing to downgrade: {existing} portfolio report runs record a monthly reporting decision."
        )
    op.execute("DROP TRIGGER portfolio_report_run_members_lock ON portfolio_report_run_members")
    op.execute("DROP FUNCTION portfolio_report_run_members_locked()")
    op.execute("DROP TRIGGER portfolio_report_runs_lock ON portfolio_report_runs")
    op.execute("DROP FUNCTION portfolio_report_runs_locked()")
    op.drop_index("ix_portfolio_report_run_members_run", table_name="portfolio_report_run_members")
    op.drop_table("portfolio_report_run_members")
    op.drop_index("ix_portfolio_report_runs_portfolio", table_name="portfolio_report_runs")
    op.drop_table("portfolio_report_runs")
