from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from monitoring_board.db import ensure_column


def normalize_nif(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def ensure_customer_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            nif TEXT,
            normalized_nif TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            review_required INTEGER NOT NULL DEFAULT 0,
            review_notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_normalized_nif
            ON customers(normalized_nif)
            WHERE COALESCE(normalized_nif, '') != '';
        CREATE INDEX IF NOT EXISTS idx_customers_active_name
            ON customers(active, name);
        """
    )
    ensure_column(conn, "assets", "customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_assets_customer ON assets(customer_id)"
    )
    backfill_customers(conn)


def backfill_customers(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT id, project_name, company_name, nif, customer_id
        FROM assets
        WHERE COALESCE(TRIM(nif), '') != ''
        ORDER BY id
        """
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        normalized = normalize_nif(row["nif"])
        if normalized:
            grouped.setdefault(normalized, []).append(row)

    created = associated = reviews = 0
    now = datetime.now().isoformat(timespec="seconds")
    for normalized_nif, assets in grouped.items():
        customer = conn.execute(
            "SELECT * FROM customers WHERE normalized_nif = ?",
            (normalized_nif,),
        ).fetchone()
        names = sorted(
            {
                str(row["company_name"] or row["project_name"] or "").strip()
                for row in assets
                if str(row["company_name"] or row["project_name"] or "").strip()
            },
            key=str.casefold,
        )
        selected_name = names[0] if names else f"Cliente {normalized_nif[-4:]}"
        review_required = len({name.casefold() for name in names}) > 1
        review_notes = (
            "Nomes divergentes nos assets associados; revisão manual necessária."
            if review_required
            else ""
        )
        if customer is None:
            customer_id = int(
                conn.execute(
                    """
                    INSERT INTO customers (
                        name, nif, normalized_nif, active, review_required,
                        review_notes, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        selected_name,
                        str(assets[0]["nif"] or "").strip(),
                        normalized_nif,
                        1 if review_required else 0,
                        review_notes,
                        now,
                        now,
                    ),
                ).lastrowid
            )
            created += 1
        else:
            customer_id = int(customer["id"])
            if review_required and not customer["review_required"]:
                conn.execute(
                    """
                    UPDATE customers
                    SET review_required = 1, review_notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (review_notes, now, customer_id),
                )
        if review_required:
            reviews += 1
        cursor = conn.execute(
            """
            UPDATE assets
            SET customer_id = ?
            WHERE id IN ({}) AND customer_id IS NULL
            """.format(", ".join("?" for _ in assets)),
            (customer_id, *(int(row["id"]) for row in assets)),
        )
        associated += cursor.rowcount
    return {"created": created, "associated": associated, "reviews": reviews}


def list_customers(conn: sqlite3.Connection, *, include_inactive: bool = False) -> list[sqlite3.Row]:
    where = "" if include_inactive else "WHERE active = 1"
    return conn.execute(
        f"""
        SELECT c.*, COUNT(a.id) AS asset_count
        FROM customers c
        LEFT JOIN assets a ON a.customer_id = c.id
        {where}
        GROUP BY c.id
        ORDER BY c.name COLLATE NOCASE, c.id
        """
    ).fetchall()
