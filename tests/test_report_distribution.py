from __future__ import annotations

import hashlib
import sqlite3

import pytest

from app import ensure_database
from monitoring_board.report_template_repository import add_generated_file, create_generation_run
from monitoring_board.reporting.distribution import (
    create_distribution,
    create_recipient,
    transition_distribution,
)
from monitoring_board.reporting.quality_gate import evaluate_report_quality
from monitoring_board.reporting.snapshots import (
    approve_snapshot,
    create_snapshot,
    validate_snapshot,
)


def connect(tmp_path) -> sqlite3.Connection:
    path = tmp_path / "distribution.db"
    ensure_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def approved_file(conn, tmp_path, *, approved: bool = True) -> tuple[int, int, int]:
    customer_id = int(
        conn.execute(
            "INSERT INTO customers (name, active, created_at, updated_at) VALUES ('Cliente Teste', 1, '2026-01-01', '2026-01-01')"
        ).lastrowid
    )
    asset_id = int(
        conn.execute(
            "INSERT INTO assets (project_name, customer_id) VALUES ('Central Distribuição', ?)",
            (customer_id,),
        ).lastrowid
    )
    payload = {
        "asset_id": asset_id,
        "energy_provider": "FusionSolar",
        "production_quality_status": "complete",
        "availability_pct": "99",
        "invoice_status": "confirmed",
    }
    snapshot_id = create_snapshot(
        conn,
        scope_type="individual",
        asset_id=asset_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        payload=payload,
        engine_version="test",
    )
    if approved:
        validate_snapshot(conn, snapshot_id, evaluate_report_quality(payload, scope="individual"))
        approve_snapshot(conn, snapshot_id, actor="tester")
    run_id = create_generation_run(
        conn,
        template_id=None,
        template_version=1,
        report_type="individual",
        asset_id=asset_id,
        snapshot_id=snapshot_id,
        period_type="monthly",
        period_start="2026-01-01",
        period_end="2026-01-31",
        requested_count=1,
    )
    root = tmp_path / "generated"
    root.mkdir(exist_ok=True)
    path = root / "report.pdf"
    content = b"synthetic report"
    path.write_bytes(content)
    file_id = add_generated_file(
        conn,
        run_id=run_id,
        fmt="pdf",
        filename=path.name,
        relative_path=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        asset_id=asset_id,
        snapshot_id=snapshot_id,
        status="completed",
    )
    return customer_id, file_id, snapshot_id


def test_recipient_and_distribution_are_audited_and_idempotent(tmp_path) -> None:
    conn = connect(tmp_path)
    customer_id, file_id, snapshot_id = approved_file(conn, tmp_path)
    recipient_id = create_recipient(
        conn,
        name="Operações",
        email="operacoes@example.test",
        customer_id=customer_id,
    )
    first = create_distribution(
        conn,
        generated_file_id=file_id,
        recipient_id=recipient_id,
        actor="tester",
        storage_root=tmp_path / "generated",
    )
    second = create_distribution(
        conn,
        generated_file_id=file_id,
        recipient_id=recipient_id,
        storage_root=tmp_path / "generated",
    )
    transition_distribution(conn, first, "approved_to_send", actor="approver")

    row = conn.execute("SELECT * FROM report_distributions WHERE id = ?", (first,)).fetchone()
    events = conn.execute(
        "SELECT event_type FROM report_distribution_events WHERE distribution_id = ?",
        (first,),
    ).fetchall()
    assert first == second
    assert row["snapshot_id"] == snapshot_id
    assert row["status"] == "approved_to_send"
    assert row["approved_by"] == "approver"
    assert [item["event_type"] for item in events] == ["prepared", "approved_to_send"]


def test_recipient_requires_valid_email_and_exactly_one_scope(tmp_path) -> None:
    conn = connect(tmp_path)
    customer_id, _file_id, _snapshot_id = approved_file(conn, tmp_path)
    with pytest.raises(ValueError, match="invalid_recipient_email"):
        create_recipient(conn, name="Inválido", email="not-an-email", customer_id=customer_id)
    with pytest.raises(ValueError, match="recipient_scope_required"):
        create_recipient(conn, name="Sem âmbito", email="ok@example.test")


def test_unapproved_snapshot_cannot_be_distributed(tmp_path) -> None:
    conn = connect(tmp_path)
    customer_id, file_id, _snapshot_id = approved_file(conn, tmp_path, approved=False)
    recipient_id = create_recipient(
        conn, name="Operações", email="ops@example.test", customer_id=customer_id
    )
    with pytest.raises(ValueError, match="snapshot_not_approved"):
        create_distribution(
            conn,
            generated_file_id=file_id,
            recipient_id=recipient_id,
            storage_root=tmp_path / "generated",
        )


def test_missing_or_corrupt_file_cannot_be_distributed(tmp_path) -> None:
    conn = connect(tmp_path)
    customer_id, file_id, _snapshot_id = approved_file(conn, tmp_path)
    recipient_id = create_recipient(
        conn, name="Operações", email="ops@example.test", customer_id=customer_id
    )
    (tmp_path / "generated" / "report.pdf").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="generated_file_integrity_failed"):
        create_distribution(
            conn,
            generated_file_id=file_id,
            recipient_id=recipient_id,
            storage_root=tmp_path / "generated",
        )


def test_invalid_distribution_transition_is_rejected(tmp_path) -> None:
    conn = connect(tmp_path)
    customer_id, file_id, _snapshot_id = approved_file(conn, tmp_path)
    recipient_id = create_recipient(
        conn, name="Operações", email="ops@example.test", customer_id=customer_id
    )
    distribution_id = create_distribution(
        conn,
        generated_file_id=file_id,
        recipient_id=recipient_id,
        storage_root=tmp_path / "generated",
    )
    with pytest.raises(ValueError, match="invalid_distribution_transition"):
        transition_distribution(conn, distribution_id, "sent", actor="tester")
