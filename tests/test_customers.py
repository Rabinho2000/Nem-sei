from __future__ import annotations

import sqlite3

from monitoring_board.customer_repository import (
    backfill_customers,
    ensure_customer_schema,
)


def legacy_connection(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "legacy-customers.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            company_name TEXT,
            nif TEXT
        )
        """
    )
    return conn


def test_customer_migration_groups_assets_by_normalized_nif(tmp_path) -> None:
    conn = legacy_connection(tmp_path)
    conn.execute(
        "INSERT INTO assets (project_name, company_name, nif) VALUES ('Norte', 'Cliente A', '599 999 991')"
    )
    conn.execute(
        "INSERT INTO assets (project_name, company_name, nif) VALUES ('Sul', 'Cliente A', '599999991')"
    )
    conn.execute(
        "INSERT INTO assets (project_name, company_name, nif) VALUES ('Sem NIF', '', '')"
    )

    ensure_customer_schema(conn)

    customers = conn.execute("SELECT * FROM customers").fetchall()
    assets = conn.execute(
        "SELECT project_name, customer_id FROM assets ORDER BY id"
    ).fetchall()
    assert len(customers) == 1
    assert customers[0]["normalized_nif"] == "599999991"
    assert assets[0]["customer_id"] == assets[1]["customer_id"] == customers[0]["id"]
    assert assets[2]["customer_id"] is None


def test_customer_migration_is_idempotent_and_flags_divergent_names(tmp_path) -> None:
    conn = legacy_connection(tmp_path)
    conn.execute(
        "INSERT INTO assets (project_name, company_name, nif) VALUES ('Norte', 'Cliente Alfa', '599999991')"
    )
    conn.execute(
        "INSERT INTO assets (project_name, company_name, nif) VALUES ('Sul', 'Cliente Beta', '599999991')"
    )

    ensure_customer_schema(conn)
    first = backfill_customers(conn)
    ensure_customer_schema(conn)

    customer = conn.execute("SELECT * FROM customers").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM assets"
    ).fetchone()[0] == 1
    assert customer["review_required"] == 1
    assert "revisão manual" in customer["review_notes"]
    assert first["created"] == 0
