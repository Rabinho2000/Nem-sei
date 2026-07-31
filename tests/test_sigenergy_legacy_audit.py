from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

import app as app_module
from monitoring_board.db import get_db
from monitoring_board.sigenergy_legacy_audit import audit_backup, main


TARGET = "25062000156"


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_legacy_audit_is_immutable_sanitized_and_classifies_system_ids_path(
    tmp_path,
) -> None:
    db_path = tmp_path / "before-expertcom-repair.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO integration_configs (
                provider, system_ids, last_error, enabled, created_at,
                updated_at
            ) VALUES ('Sigenergy', ?, ?, 1, ?, ?)
            """,
            (
                TARGET,
                f"/openapi/systems/{TARGET}/energyFlow failed",
                now,
                now,
            ),
        )
        payload = {
            "system": {
                "systemId": TARGET,
                "systemName": TARGET,
            },
            "energy_flow": {"pvPower": 1.5},
            "accessToken": "must-not-appear",
        }
        for collected_at in (
            "2026-07-29T10:00:00",
            "2026-07-30T10:00:00",
        ):
            conn.execute(
                """
                INSERT INTO integration_realtime_snapshots (
                    asset_id, provider, external_id, collected_at,
                    payload_json
                ) VALUES (NULL, 'Sigenergy', ?, ?, ?)
                """,
                (
                    TARGET,
                    collected_at,
                    json.dumps(payload),
                ),
            )
        conn.execute(
            """
            INSERT INTO integration_sync_runs (
                provider, trigger_type, status, started_at, finished_at
            ) VALUES (
                'Sigenergy', 'scheduled', 'success',
                '2026-07-29T09:59:00', '2026-07-29T10:01:00'
            )
            """
        )
        app_module.create_background_job(
            conn,
            "sigenergy_state_sync",
            {
                "provider": "Sigenergy",
                "target_external_ids": [TARGET],
            },
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    before = _file_sha256(db_path)
    result = audit_backup(db_path, TARGET)
    after = _file_sha256(db_path)
    rendered = json.dumps(result)

    assert before == after
    assert result["mode"] == "sqlite_read_only_immutable"
    assert result["database"]["quick_check"] == "ok"
    assert result["snapshots"]["snapshot_count"] == 2
    assert result["snapshots"]["asset_ids"] == [None]
    assert (
        result["snapshots"]["boundaries"]["first"]["payload"][
            "synthetic_identity_signature"
        ]
        is True
    )
    assert result["integration_config"]["target_in_legacy_system_ids"] is True
    assert (
        result["origin_classification"]["conclusion"]
        == "legacy_system_ids_synthetic_inventory_path"
    )
    assert result["origin_classification"]["confidence"] == "high"
    assert "must-not-appear" not in rendered


def test_legacy_audit_recovers_system_ids_path_after_value_was_cleared(
    tmp_path,
) -> None:
    db_path = tmp_path / "before-repair-with-cleared-config.db"
    app_module.ensure_database(str(db_path))
    with get_db(str(db_path)) as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO integration_configs (
                provider, system_ids, last_error, enabled, created_at,
                updated_at
            ) VALUES ('Sigenergy', '', ?, 1, ?, ?)
            """,
            (
                f"/openapi/systems/{TARGET}/energyFlow failed",
                now,
                now,
            ),
        )
        payload = {
            "system": {"systemId": TARGET, "systemName": TARGET},
            "realtime": {},
            "energy_flow": {},
            "fetch_error": "access denied",
            "fetch_status": "error",
        }
        cases = (
            ("scheduled_state", "2026-07-29T10:00:00"),
            ("manual_background", "2026-07-30T10:00:00"),
        )
        for trigger_type, collected_at in cases:
            conn.execute(
                """
                INSERT INTO integration_realtime_snapshots (
                    asset_id, provider, external_id, collected_at,
                    payload_json
                ) VALUES (NULL, 'Sigenergy', ?, ?, ?)
                """,
                (TARGET, collected_at, json.dumps(payload)),
            )
            conn.execute(
                """
                INSERT INTO integration_sync_runs (
                    provider, trigger_type, status, started_at, finished_at,
                    summary_json
                ) VALUES ('Sigenergy', ?, 'partial', ?, ?, ?)
                """,
                (
                    trigger_type,
                    collected_at,
                    collected_at,
                    json.dumps(
                        {
                            "provider_rows": 1,
                            "energy_flow_error": f"{TARGET}: access denied",
                        }
                    ),
                ),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    result = audit_backup(db_path, TARGET)

    assert result["audit_version"] == 2
    assert len(result["integration_runs"]) == 2
    assert result["integration_config"]["legacy_system_ids_present"] is False
    classification = result["origin_classification"]
    assert (
        classification["conclusion"]
        == "legacy_system_ids_synthetic_inventory_path"
    )
    assert classification["confidence"] == "high"
    assert (
        classification["configured_value_source"]
        == "database_or_environment_not_recoverable"
    )
    assert classification["evidence"]["target_linked_runs_cover_snapshots"]
    assert classification["evidence"]["target_linked_run_triggers"] == {
        "manual_background": 1,
        "scheduled_state": 1,
    }


def test_legacy_audit_refuses_live_database_filename(tmp_path) -> None:
    db_path = tmp_path / "monitoring_board.db"
    app_module.ensure_database(str(db_path))

    with pytest.raises(ValueError, match="recusa o nome da base live"):
        audit_backup(db_path, TARGET)


def test_legacy_audit_cli_cannot_overwrite_the_audited_backup(
    tmp_path,
    capsys,
) -> None:
    db_path = tmp_path / "safe-backup.db"
    app_module.ensure_database(str(db_path))
    before = _file_sha256(db_path)

    result = main(
        [
            "--database",
            str(db_path),
            "--system-id",
            TARGET,
            "--output",
            str(db_path),
        ]
    )

    assert result == 2
    assert _file_sha256(db_path) == before
    assert "nao pode ser o proprio backup" in capsys.readouterr().err
