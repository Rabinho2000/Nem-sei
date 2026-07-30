from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app import ensure_database
from monitoring_board.db import get_db
from monitoring_board.sigenergy_preview_worker import (
    EXPERTCOM_SIGENERGY_SYSTEM_ID,
    backfill,
    discover,
    validate_worker_environment,
)


HISTORY_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "sigenergy"
    / "system_history_day_kwh.json"
)


def configure_worker_environment(monkeypatch) -> None:
    values = {
        "APP_ENV": "preview",
        "PREVIEW_BANNER": "true",
        "EXTERNAL_ACTIONS_ENABLED": "false",
        "SCHEDULER_ENABLED": "false",
        "FUSIONSOLAR_PRODUCTION_SYNC_ENABLED": "false",
        "FUSIONSOLAR_DIAGNOSTICS_SYNC_ENABLED": "false",
        "TELEGRAM_ALERTS_ENABLED": "false",
        "TELEGRAM_DAILY_SUMMARY_ENABLED": "false",
        "SIGENERGY_BASE_URL": "https://api-eu.sigencloud.com",
        "SIGENERGY_REGION": "eu",
        "SIGENERGY_HISTORY_ENERGY_UNIT": "kWh",
        "SIGENERGY_ALLOWED_SYSTEM_IDS": EXPERTCOM_SIGENERGY_SYSTEM_ID,
        "SIGENERGY_APP_KEY": "fixture-key",
        "SIGENERGY_APP_SECRET": "fixture-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_worker_environment_requires_exact_base_url_and_system_allowlist(
    monkeypatch,
) -> None:
    configure_worker_environment(monkeypatch)
    validate_worker_environment()

    monkeypatch.setenv("SIGENERGY_ALLOWED_SYSTEM_IDS", "OTHER-SYSTEM")
    with pytest.raises(ValueError, match="allowlist"):
        validate_worker_environment()

    monkeypatch.setenv(
        "SIGENERGY_ALLOWED_SYSTEM_IDS",
        EXPERTCOM_SIGENERGY_SYSTEM_ID,
    )
    monkeypatch.setenv("SIGENERGY_BASE_URL", "https://example.invalid")
    with pytest.raises(ValueError, match="Base URL"):
        validate_worker_environment()


def test_worker_discovery_updates_inventory_without_creating_assets(
    tmp_path,
) -> None:
    db_path = tmp_path / "preview-discovery.db"
    ensure_database(str(db_path))

    class Client:
        def list_systems(self):
            return [
                {
                    "systemId": EXPERTCOM_SIGENERGY_SYSTEM_ID,
                    "systemName": "Expertcom",
                    "status": "running",
                }
            ]

    with get_db(str(db_path)) as conn:
        result = discover(conn, Client())
        inventory = conn.execute(
            "SELECT * FROM provider_system_inventory"
        ).fetchall()
        asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    assert result["assets_created"] == 0
    assert len(inventory) == 1
    assert inventory[0]["external_name"] == "Expertcom"
    assert asset_count == 0


def test_worker_backfill_is_sequential_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "preview-backfill.db"
    ensure_database(str(db_path))
    payload = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))["data"]
    calls: list[str] = []

    class Client:
        def get_system_history(
            self,
            system_id,
            *,
            level,
            target_date,
        ):
            assert system_id == EXPERTCOM_SIGENERGY_SYSTEM_ID
            assert level == "Day"
            calls.append(target_date)
            return payload

    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        asset_id = int(
            conn.execute(
                "INSERT INTO assets (project_name) VALUES ('Expertcom')"
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO provider_system_inventory (
                provider, external_id, external_name, metadata_json,
                access_status, first_discovered_at, last_discovered_at,
                data_quality, created_at, updated_at
            ) VALUES (
                'Sigenergy', ?, 'Expertcom', '{}', 'accessible',
                ?, ?, 'missing', ?, ?
            )
            """,
            (EXPERTCOM_SIGENERGY_SYSTEM_ID, now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO asset_integrations (
                asset_id, provider, external_id, external_name, enabled,
                is_primary_energy_source
            ) VALUES (?, 'Sigenergy', ?, 'Expertcom', 1, 0)
            """,
            (asset_id, EXPERTCOM_SIGENERGY_SYSTEM_ID),
        )
        conn.commit()
        first = backfill(
            conn,
            Client(),
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 2),
            minimum_interval_seconds=0,
        )
        second = backfill(
            conn,
            Client(),
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 2),
            minimum_interval_seconds=0,
        )
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM energy_interval_facts"
        ).fetchone()[0]
        daily_count = conn.execute(
            """
            SELECT COUNT(*) FROM production_records
            WHERE period_type = 'day' AND provider = 'Sigenergy'
            """
        ).fetchone()[0]
        month_quality = conn.execute(
            """
            SELECT data_quality FROM production_records
            WHERE period_type = 'month' AND provider = 'Sigenergy'
            """
        ).fetchone()["data_quality"]

    assert first["completed"] == ["2026-02-01", "2026-02-02"]
    assert second["failed"] == []
    assert calls == [
        "2026-02-01",
        "2026-02-02",
        "2026-02-01",
        "2026-02-02",
    ]
    assert fact_count == 2
    assert daily_count == 2
    assert month_quality == "partial"
