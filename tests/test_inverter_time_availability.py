from __future__ import annotations

from datetime import date, datetime, timedelta

import app as app_module
from app import app as flask_app
from monitoring_board.db import get_db


def test_inverter_and_weighted_plant_availability() -> None:
    valid_slots = {
        datetime(2026, 6, 1, 8, 0) + timedelta(minutes=15 * index)
        for index in range(8)
    }
    result = app_module.calculate_inverter_daily_availability(
        [
            {"sample_time": datetime(2026, 6, 1, 8, 1), "active_power_kw": 10},
            {"sample_time": datetime(2026, 6, 1, 8, 16), "active_power_kw": 5},
            {"sample_time": datetime(2026, 6, 1, 8, 31), "active_power_kw": 5},
            {"sample_time": datetime(2026, 6, 1, 8, 46), "active_power_kw": 5},
            {"sample_time": datetime(2026, 6, 1, 9, 1), "active_power_kw": 0},
            {"sample_time": datetime(2026, 6, 1, 9, 16), "active_power_kw": 0},
        ],
        valid_slots,
    )

    assert result == {
        "valid_slots": 4,
        "available_slots": 2,
        "unavailable_slots": 2,
        "availability_pct": 50.0,
    }
    assert app_module.calculate_weighted_plant_availability(
        [
            {"availability_pct": 100.0, "inverter_power_kw": 50.0},
            {"availability_pct": 50.0, "inverter_power_kw": 50.0},
        ]
    ) == 75.0


def test_normalize_history_rows_keeps_zero_power_and_raw_payload() -> None:
    device = {
        "asset_id": 1,
        "station_code": "S1",
        "external_device_id": "I1",
        "dev_dn": "DN1",
        "sn": "SN1",
        "device_name": "Inversor 1",
        "dev_type_id": 1,
        "rated_power_kw": 50.0,
    }
    raw_sample = {"collectTime": 1780300800000, "dataItemMap": {"active_power": 0}}

    rows = app_module.normalize_fusionsolar_device_history_rows(
        [{"devId": "I1", "dataItemMap": [raw_sample]}],
        [device],
    )

    assert len(rows) == 1
    assert rows[0]["active_power_kw"] == 0
    assert rows[0]["raw_payload"] == raw_sample


def test_sync_excludes_all_zero_slots_and_is_idempotent(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "inverter-availability.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    conn.execute("INSERT INTO assets (id, project_name, kwp) VALUES (1, 'Central A', '100')")
    conn.commit()
    target_date = date.today() - timedelta(days=1)
    devices = [
        {
            "asset_id": 1,
            "station_code": "S1",
            "external_device_id": "I1",
            "dev_dn": "I1",
            "sn": "SN1",
            "device_name": "Inversor 1",
            "dev_type_id": 1,
            "rated_power_kw": 50.0,
        },
        {
            "asset_id": 1,
            "station_code": "S1",
            "external_device_id": "I2",
            "dev_dn": "I2",
            "sn": "SN2",
            "device_name": "Inversor 2",
            "dev_type_id": 1,
            "rated_power_kw": 50.0,
        },
        {
            "asset_id": 1,
            "station_code": "S1",
            "external_device_id": "I3",
            "dev_dn": "I3",
            "sn": "SN3",
            "device_name": "Inversor 3",
            "dev_type_id": 1,
            "rated_power_kw": 100.0,
        },
    ]
    context = {
        "provider": "FusionSolar",
        "session": object(),
        "endpoints": {"base_url": "https://fusion.test", "device_history_endpoint": "/history"},
        "devices": devices,
    }

    def fake_history(*_args, **_kwargs):
        rows = []
        for inverter_id, powers in (
            ("I1", [10, 10, 10, 10, 10, 10, 10, 10]),
            ("I2", [0, 0, 10, 10, 0, 0, 0, 0]),
        ):
            device = next(item for item in devices if item["external_device_id"] == inverter_id)
            for index, power in enumerate(powers):
                rows.append(
                    {
                        **device,
                        "sample_time": datetime.combine(target_date, datetime.min.time()).replace(hour=8)
                        + timedelta(minutes=15 * index),
                        "active_power_kw": power,
                        "raw_payload": {"active_power": power},
                    }
                )
        return rows

    monkeypatch.setattr(app_module, "fetch_fusionsolar_device_history", fake_history)
    try:
        app_module.sync_fusionsolar_inverter_availability_for_date(conn, target_date, context=context)
        app_module.sync_fusionsolar_inverter_availability_for_date(conn, target_date, context=context)

        inverter_rows = conn.execute(
            "SELECT * FROM inverter_availability_daily ORDER BY inverter_id"
        ).fetchall()
        plant_row = conn.execute("SELECT * FROM plant_availability_daily").fetchone()

        assert conn.execute("SELECT COUNT(*) FROM inverter_power_samples").fetchone()[0] == 16
        assert conn.execute("SELECT COUNT(*) FROM inverter_availability_daily").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM plant_availability_daily").fetchone()[0] == 1
        assert len(inverter_rows) == 3
        assert inverter_rows[0]["valid_slots"] == 4
        assert inverter_rows[0]["availability_pct"] == 100.0
        assert inverter_rows[1]["availability_pct"] == 50.0
        assert inverter_rows[2]["valid_slots"] == 4
        assert inverter_rows[2]["available_slots"] == 0
        assert inverter_rows[2]["availability_pct"] == 0.0
        assert plant_row["valid_slots"] == 4
        assert plant_row["weighted_availability_pct"] == 37.5
    finally:
        conn.close()


def test_daily_wat_report_data_returns_ok_partial_and_no_data(tmp_path) -> None:
    db_path = tmp_path / "daily-wat-report.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    target_date = date(2026, 6, 14)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        asset_ok = conn.execute("INSERT INTO assets (project_name) VALUES ('Central OK')").lastrowid
        asset_partial = conn.execute("INSERT INTO assets (project_name) VALUES ('Central Parcial')").lastrowid
        asset_empty = conn.execute("INSERT INTO assets (project_name) VALUES ('Central Sem Dados')").lastrowid
        conn.executemany(
            """
            INSERT INTO provider_devices (
                asset_id, provider, station_code, external_device_id, device_name, dev_type_id,
                rated_power_kw, enabled, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, ?, ?, 1, ?, 1, ?, ?)
            """,
            [
                (asset_ok, "S1", "OK-1", "OK Inversor 1", 60, now, now),
                (asset_ok, "S1", "OK-2", "OK Inversor 2", 40, now, now),
                (asset_partial, "S2", "P-1", "Parcial Inversor", 50, now, now),
                (asset_empty, "S3", "E-1", "Sem Dados Inversor", 50, now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO inverter_availability_daily (
                asset_id, provider, availability_date, inverter_id, inverter_name, inverter_power_kw,
                valid_slots, available_slots, unavailable_slots, availability_pct, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, ?, ?, ?, 10, ?, ?, ?, ?, ?)
            """,
            [
                (asset_ok, target_date.isoformat(), "OK-1", "OK Inversor 1", 60, 10, 0, 100, now, now),
                (asset_ok, target_date.isoformat(), "OK-2", "OK Inversor 2", 40, 8, 2, 80, now, now),
                (asset_partial, target_date.isoformat(), "P-1", "Parcial Inversor", 50, 5, 5, 50, now, now),
            ],
        )
        conn.execute(
            """
            INSERT INTO plant_availability_daily (
                asset_id, provider, availability_date, valid_slots, weighted_availability_pct,
                inverter_count, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, 10, 92, 2, ?, ?)
            """,
            (asset_ok, target_date.isoformat(), now, now),
        )
        conn.executemany(
            """
            INSERT INTO inverter_power_samples (
                asset_id, provider, external_station_id, inverter_id, inverter_name,
                sample_time, active_power_kw, created_at
            ) VALUES (?, 'FusionSolar', ?, ?, ?, ?, ?, ?)
            """,
            [
                (asset_ok, "S1", "OK-1", "OK Inversor 1", f"{target_date}T12:00:00", 10, now),
                (asset_ok, "S1", "OK-2", "OK Inversor 2", f"{target_date}T12:00:00", 0, now),
                (asset_partial, "S2", "P-1", "Parcial Inversor", f"{target_date}T12:00:00", 5, now),
            ],
        )
        conn.commit()

        report = app_module.get_daily_wat_report_data(conn, target_date)
    finally:
        conn.close()

    assert report["target_date"] == "2026-06-14"
    plants = {row["project_name"]: row for row in report["plants"]}
    assert plants["Central OK"] == {
        "asset_id": asset_ok,
        "project_name": "Central OK",
        "weighted_wat_pct": 92.0,
        "inverter_count": 2,
        "inverters_below_90_count": 1,
        "worst_inverter": "OK Inversor 2",
        "worst_inverter_id": "OK-2",
        "worst_inverter_wat_pct": 80.0,
        "valid_slots": 20,
        "unavailable_slots": 2,
        "data_status": "ok",
        "warnings": [],
    }
    assert plants["Central Parcial"]["data_status"] == "parcial"
    assert plants["Central Parcial"]["weighted_wat_pct"] == 50.0
    assert plants["Central Sem Dados"]["data_status"] == "sem dados"
    assert plants["Central Sem Dados"]["inverter_count"] == 1


def test_monthly_wat_report_data_aggregates_range_and_filters_asset(tmp_path) -> None:
    db_path = tmp_path / "monthly-wat-report.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    from_date = date(2026, 6, 13)
    to_date = date(2026, 6, 14)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        asset_id = conn.execute("INSERT INTO assets (project_name) VALUES ('Central Mensal')").lastrowid
        other_asset_id = conn.execute("INSERT INTO assets (project_name) VALUES ('Outra Central')").lastrowid
        conn.executemany(
            """
            INSERT INTO provider_devices (
                asset_id, provider, station_code, external_device_id, device_name, dev_type_id,
                rated_power_kw, enabled, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, ?, ?, 1, ?, 1, ?, ?)
            """,
            [
                (asset_id, "S1", "M-1", "Mensal 1", 75, now, now),
                (asset_id, "S1", "M-2", "Mensal 2", 25, now, now),
                (other_asset_id, "S2", "O-1", "Outro 1", 50, now, now),
            ],
        )
        for current_date in (from_date, to_date):
            conn.executemany(
                """
                INSERT INTO inverter_availability_daily (
                    asset_id, provider, availability_date, inverter_id, inverter_name, inverter_power_kw,
                    valid_slots, available_slots, unavailable_slots, availability_pct, created_at, updated_at
                ) VALUES (?, 'FusionSolar', ?, ?, ?, ?, 10, ?, ?, ?, ?, ?)
                """,
                [
                    (asset_id, current_date.isoformat(), "M-1", "Mensal 1", 75, 9, 1, 90, now, now),
                    (asset_id, current_date.isoformat(), "M-2", "Mensal 2", 25, 8, 2, 80, now, now),
                ],
            )
            conn.execute(
                """
                INSERT INTO plant_availability_daily (
                    asset_id, provider, availability_date, valid_slots, weighted_availability_pct,
                    inverter_count, created_at, updated_at
                ) VALUES (?, 'FusionSolar', ?, 10, 87.5, 2, ?, ?)
                """,
                (asset_id, current_date.isoformat(), now, now),
            )
            conn.executemany(
                """
                INSERT INTO inverter_power_samples (
                    asset_id, provider, external_station_id, inverter_id, inverter_name,
                    sample_time, active_power_kw, created_at
                ) VALUES (?, 'FusionSolar', 'S1', ?, ?, ?, ?, ?)
                """,
                [
                    (asset_id, "M-1", "Mensal 1", f"{current_date}T12:00:00", 10, now),
                    (asset_id, "M-2", "Mensal 2", f"{current_date}T12:00:00", 5, now),
                ],
            )
        conn.commit()

        report = app_module.get_monthly_wat_report_data(conn, from_date, to_date, asset_id=asset_id)
    finally:
        conn.close()

    assert report["from_date"] == "2026-06-13"
    assert report["to_date"] == "2026-06-14"
    assert len(report["plants"]) == 1
    plant = report["plants"][0]
    assert plant["asset_id"] == asset_id
    assert plant["weighted_wat_pct"] == 87.5
    assert plant["inverter_count"] == 2
    assert plant["inverters_below_90_count"] == 1
    assert plant["worst_inverter"] == "Mensal 2"
    assert plant["worst_inverter_wat_pct"] == 80.0
    assert plant["valid_slots"] == 40
    assert plant["unavailable_slots"] == 6
    assert plant["data_status"] == "ok"


def test_performance_page_renders_time_availability_and_empty_state(tmp_path) -> None:
    db_path = tmp_path / "performance-time.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    yesterday = date.today() - timedelta(days=1)
    try:
        asset_id = conn.execute(
            "INSERT INTO assets (project_name, kwp, active_contract) VALUES ('Central A', '100', 'yes')"
        ).lastrowid
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO inverter_availability_daily (
                asset_id, provider, availability_date, inverter_id, inverter_name, inverter_power_kw,
                valid_slots, available_slots, unavailable_slots, availability_pct, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, 'I1', 'Inversor 1', 50, 4, 3, 1, 75, ?, ?)
            """,
            (asset_id, yesterday.isoformat(), now, now),
        )
        conn.execute(
            """
            INSERT INTO inverter_power_samples (
                asset_id, provider, external_station_id, inverter_id, inverter_name,
                sample_time, active_power_kw, created_at
            ) VALUES (?, 'FusionSolar', 'S1', 'I1', 'Inversor 1', ?, 10, ?)
            """,
            (asset_id, f"{yesterday.isoformat()}T12:00:00", now),
        )
        conn.commit()
    finally:
        conn.close()

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "admin"
        response = client.get("/performance?om_only=yes")
        empty_response = client.get("/performance?period=custom&from_date=2020-01-01&to_date=2020-01-01")
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert response.status_code == 200
    assert "WAT — Disponibilidade Ponderada dos Inversores".encode() in response.data
    assert b"75.0%" in response.data
    assert b"Inversor 1" in response.data
    assert b"Inversores abaixo de 90%" in response.data
    assert b"Pior central" in response.data
    assert b"Ranking de centrais por WAT" in response.data
    assert b"Ranking de inversores por WAT" in response.data
    assert "Sem dados WAT para este periodo".encode() in empty_response.data


def test_clicking_plant_opens_daily_inverter_charts(tmp_path) -> None:
    db_path = tmp_path / "performance-charts.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    yesterday = date.today() - timedelta(days=1)
    previous_day = yesterday - timedelta(days=1)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        asset_id = conn.execute(
            "INSERT INTO assets (project_name, kwp, active_contract) VALUES ('Central Graficos', '100', 'yes')"
        ).lastrowid
        conn.executemany(
            """
            INSERT INTO inverter_availability_daily (
                asset_id, provider, availability_date, inverter_id, inverter_name, inverter_power_kw,
                valid_slots, available_slots, unavailable_slots, availability_pct, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, 'I1', 'Inversor Grafico', 50, 10, ?, ?, ?, ?, ?)
            """,
            [
                (asset_id, previous_day.isoformat(), 8, 2, 80, now, now),
                (asset_id, yesterday.isoformat(), 10, 0, 100, now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO provider_devices (
                asset_id, provider, station_code, external_device_id, device_name, dev_type_id,
                rated_power_kw, enabled, created_at, updated_at
            ) VALUES (?, 'FusionSolar', 'S1', ?, ?, 1, 50, 1, ?, ?)
            """,
            [
                (asset_id, "I1", "Inversor Grafico", now, now),
                (asset_id, "I2", "Inversor Sem Amostras", now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO inverter_power_samples (
                asset_id, provider, external_station_id, inverter_id, inverter_name,
                inverter_power_kw, sample_time, active_power_kw, created_at
            ) VALUES (?, 'FusionSolar', 'S1', 'I1', 'Inversor Grafico', 50, ?, ?, ?)
            """,
            [
                (asset_id, f"{yesterday.isoformat()}T08:00:00", 0, now),
                (asset_id, f"{yesterday.isoformat()}T10:00:00", 25, now),
                (asset_id, f"{yesterday.isoformat()}T12:00:00", 48, now),
                (asset_id, f"{yesterday.isoformat()}T16:00:00", 10, now),
                (asset_id, f"{yesterday.isoformat()}T18:00:00", 0, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "admin"
        ranking = client.get(
            f"/performance?period=custom&from_date={previous_day}&to_date={yesterday}"
        )
        detail = client.get(
            f"/performance?asset_id={asset_id}&period=custom&from_date={previous_day}&to_date={yesterday}"
        )
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert ranking.status_code == 200
    assert f"asset_id={asset_id}".encode() in ranking.data
    assert detail.status_code == 200
    assert b"Detalhe da instalacao" in detail.data
    assert b"Central Graficos" in detail.data
    assert b"Inversor Grafico" in detail.data
    assert b"Inversor Sem Amostras" in detail.data
    assert b"power-chart-area" in detail.data
    assert b"power-chart-line" in detail.data
    assert b"Sem amostras de potencia ativa neste periodo" in detail.data
    assert b"48.0 kW" not in detail.data


def test_build_inverter_power_chart_creates_line_and_area_paths() -> None:
    start = datetime(2026, 6, 1)
    chart = app_module.build_inverter_power_chart(
        [
            (start.replace(hour=8), 0.0),
            (start.replace(hour=12), 10.0),
            (start.replace(hour=18), 0.0),
        ],
        start,
        start + timedelta(days=1),
        15.0,
    )

    assert chart["sample_count"] == 3
    assert chart["line_path"].startswith("M ")
    assert chart["area_path"].endswith(" Z")
    assert chart["max_power_kw"] == 15.0
    assert [tick["label"] for tick in chart["x_ticks"]] == ["00:00", "06:00", "12:00", "18:00", "00:00"]


def test_report_excludes_removed_inverter_and_weights_power_from_model(tmp_path) -> None:
    db_path = tmp_path / "removed-inverter.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    target_date = date(2026, 6, 14)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        asset_id = conn.execute(
            "INSERT INTO assets (project_name, kwp) VALUES ('Oliveira do Douro', '250')"
        ).lastrowid
        devices = [
            ("I1", "Inversor 1", "SUN2000-100KTL-M2"),
            ("I2", "Inversor 2", "SUN2000-100KTL-M2"),
            ("I3", "Inversor 3", "SUN2000-50KTL-M3"),
            ("IR", "inversor (removido)", "SUN2000-100KTL-M2"),
        ]
        conn.executemany(
            """
            INSERT INTO provider_devices (
                asset_id, provider, station_code, external_device_id, device_name, dev_type_id,
                model, enabled, created_at, updated_at
            ) VALUES (?, 'FusionSolar', 'S1', ?, ?, 1, ?, 1, ?, ?)
            """,
            [(asset_id, inverter_id, name, model, now, now) for inverter_id, name, model in devices],
        )
        availability = {"I1": 100.0, "I2": 0.0, "I3": 96.61, "IR": 0.0}
        conn.executemany(
            """
            INSERT INTO inverter_availability_daily (
                asset_id, provider, availability_date, inverter_id, inverter_name,
                valid_slots, available_slots, unavailable_slots, availability_pct, created_at, updated_at
            ) VALUES (?, 'FusionSolar', ?, ?, ?, 59, ?, ?, ?, ?, ?)
            """,
            [
                (
                    asset_id,
                    target_date.isoformat(),
                    inverter_id,
                    name,
                    round(59 * availability[inverter_id] / 100),
                    59 - round(59 * availability[inverter_id] / 100),
                    availability[inverter_id],
                    now,
                    now,
                )
                for inverter_id, name, _model in devices
            ],
        )
        conn.executemany(
            """
            INSERT INTO inverter_power_samples (
                asset_id, provider, external_station_id, inverter_id, inverter_name,
                sample_time, active_power_kw, created_at
            ) VALUES (?, 'FusionSolar', 'S1', ?, ?, ?, ?, ?)
            """,
            [
                (asset_id, inverter_id, name, f"{target_date.isoformat()}T12:00:00", 0, now)
                for inverter_id, name, _model in devices
                if inverter_id != "IR"
            ],
        )
        conn.commit()

        report = app_module.get_inverter_availability_report(
            conn,
            target_date,
            target_date,
            asset_id=asset_id,
            om_only=False,
        )
        charts = app_module.get_inverter_availability_chart_report(conn, asset_id, target_date, target_date)
    finally:
        conn.close()

    assert len(report["inverters"]) == 3
    assert all("removido" not in row["inverter_name"] for row in report["inverters"])
    assert report["plants"][0]["availability_pct"] == 59.32
    assert [row["inverter_power_kw"] for row in report["inverters"]] == [100.0, 50.0, 100.0]
    assert charts is not None
    assert len(charts["inverters"]) == 3


def test_infer_inverter_power_from_model() -> None:
    assert app_module.infer_inverter_power_from_model("SUN2000-100KTL-M2") == 100.0
    assert app_module.infer_inverter_power_from_model("SUN2000-50KTL-M3") == 50.0
    assert app_module.infer_inverter_power_from_model("SUN2000-20K-MB0") == 20.0
    assert app_module.infer_inverter_power_from_model("unknown") is None


def test_edge_tolerance_ignores_first_and_last_30_minutes() -> None:
    start = datetime(2026, 6, 1, 8)
    valid_slots = {start + timedelta(minutes=15 * index) for index in range(8)}
    delayed_samples = [
        {"sample_time": start + timedelta(minutes=15 * index), "active_power_kw": 10}
        for index in range(2, 6)
    ]

    result = app_module.calculate_inverter_daily_availability(delayed_samples, valid_slots)

    assert result["valid_slots"] == 4
    assert result["available_slots"] == 4
    assert result["unavailable_slots"] == 0
    assert result["availability_pct"] == 100.0


def test_performance_filters_by_search_and_om_contract(tmp_path) -> None:
    db_path = tmp_path / "performance-filters.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    target_date = date.today() - timedelta(days=1)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        om_asset = conn.execute(
            """
            INSERT INTO assets (project_name, company_name, location, kwp, active_contract)
            VALUES ('Central Solar Norte', 'Cliente Alfa', 'Porto', '50', 'yes')
            """
        ).lastrowid
        other_asset = conn.execute(
            """
            INSERT INTO assets (project_name, company_name, location, kwp, active_contract)
            VALUES ('Central Solar Sul', 'Cliente Beta', 'Faro', '50', 'no')
            """
        ).lastrowid
        for asset_id, inverter_id in ((om_asset, "I1"), (other_asset, "I2")):
            conn.execute(
                """
                INSERT INTO inverter_availability_daily (
                    asset_id, provider, availability_date, inverter_id, inverter_name,
                    inverter_power_kw, valid_slots, available_slots, unavailable_slots,
                    availability_pct, created_at, updated_at
                ) VALUES (?, 'FusionSolar', ?, ?, ?, 50, 10, 10, 0, 100, ?, ?)
                """,
                (asset_id, target_date.isoformat(), inverter_id, inverter_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO inverter_power_samples (
                    asset_id, provider, external_station_id, inverter_id, inverter_name,
                    sample_time, active_power_kw, created_at
                ) VALUES (?, 'FusionSolar', 'S1', ?, ?, ?, 10, ?)
                """,
                (asset_id, inverter_id, inverter_id, f"{target_date.isoformat()}T12:00:00", now),
            )
        conn.commit()
    finally:
        conn.close()

    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "admin"
        om_response = client.get("/performance?om_only=yes")
        all_response = client.get("/performance?om_only=no")
        search_response = client.get("/performance?om_only=no&search=Faro")
    finally:
        flask_app.config["DATABASE"] = previous_db

    assert b"Central Solar Norte" in om_response.data
    assert b"Central Solar Sul" not in om_response.data
    assert b"Central Solar Norte" in all_response.data
    assert b"Central Solar Sul" in all_response.data
    assert b"Central Solar Sul" in search_response.data
    assert b"Central Solar Norte" not in search_response.data
    assert b'name="search" value="Faro"' in search_response.data
    assert b'<option value="no" selected>Todas</option>' in search_response.data


def test_wat_post_queues_background_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "wat-background.db"
    app_module.ensure_database(str(db_path))
    scheduled: list[int] = []
    monkeypatch.setattr(
        app_module,
        "sync_fusionsolar_inverter_availability_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run inline")),
    )
    monkeypatch.setattr(app_module, "schedule_background_job", lambda _app, job_id: scheduled.append(job_id) or True)
    previous_db = flask_app.config["DATABASE"]
    flask_app.config["DATABASE"] = str(db_path)
    try:
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = "admin"
            session["csrf_token"] = "token"
        target_date = date.today() - timedelta(days=1)
        response = client.post(
            "/performance",
            data={
                "csrf_token": "token",
                "action": "sync_inverter_time_availability",
                "from_date": target_date.isoformat(),
                "to_date": target_date.isoformat(),
                "search": "Central",
                "om_only": "yes",
            },
        )
    finally:
        flask_app.config["DATABASE"] = previous_db

    conn = get_db(str(db_path))
    try:
        job = conn.execute("SELECT * FROM background_jobs").fetchone()
        assert response.status_code == 302
        assert job["job_type"] == "fusionsolar_inverter_availability_backfill"
        assert scheduled == [job["id"]]
    finally:
        conn.close()


def test_wat_backfill_waits_and_continues_after_rate_limit(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "wat-rate-limit.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    target_date = date.today() - timedelta(days=1)
    calls = {"sync": 0}
    sleeps: list[float] = []
    context = {
        "devices": [{"external_device_id": "I1"}],
        "config": {},
        "history_call_delay_seconds": 0,
        "sleeper": sleeps.append,
    }
    monkeypatch.setattr(app_module, "prepare_fusionsolar_inverter_history_context", lambda *_args, **_kwargs: context)

    def fake_sync(*_args, **_kwargs):
        calls["sync"] += 1
        if calls["sync"] == 1:
            raise ValueError("API call limit exceeded (failCode=407)")
        return {"samples": 10, "plants": 1, "inverters": 1}

    monkeypatch.setattr(app_module, "sync_fusionsolar_inverter_availability_for_date", fake_sync)
    try:
        result = app_module.run_fusionsolar_inverter_availability_backfill(
            conn,
            from_date=target_date,
            to_date=target_date,
            sleeper=sleeps.append,
            history_call_delay_seconds=0,
        )
    finally:
        app_module.FUSIONSOLAR_PERFORMANCE_RATE_LIMIT_UNTIL = None
        conn.close()

    assert calls["sync"] == 2
    assert result["days"] == 1
    assert result["wait_cycles"] == 1
    assert result["stopped_reason"] == ""
    assert sleeps and sleeps[0] > 0


def test_background_payload_dispatches_wat_backfill(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "wat-payload.db"
    app_module.ensure_database(str(db_path))
    conn = get_db(str(db_path))
    captured: dict[str, date] = {}

    def fake_backfill(_conn, *, from_date, to_date, **_kwargs):
        captured.update(from_date=from_date, to_date=to_date)
        return {"days": 1}

    monkeypatch.setattr(app_module, "run_fusionsolar_inverter_availability_backfill", fake_backfill)
    try:
        result = app_module.run_background_job_payload(
            conn,
            "fusionsolar_inverter_availability_backfill",
            {"from_date": "2026-06-14", "to_date": "2026-06-14"},
        )
    finally:
        conn.close()

    assert result == {"days": 1}
    assert captured == {"from_date": date(2026, 6, 14), "to_date": date(2026, 6, 14)}
