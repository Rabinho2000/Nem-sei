from __future__ import annotations

from monitoring_board.db import ensure_column, get_db, query_all, query_scalar


def test_db_helpers_enable_foreign_keys_and_query_rows(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES (?)", ("Central A",))
        conn.commit()

        rows = query_all(conn, "SELECT * FROM items")

        assert query_scalar(conn, "PRAGMA foreign_keys") == 1
        assert rows[0]["name"] == "Central A"
        assert query_scalar(conn, "SELECT COUNT(*) FROM items") == 1
    finally:
        conn.close()


def test_ensure_column_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "test.db"
    conn = get_db(str(db_path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")

        ensure_column(conn, "items", "notes TEXT")
        ensure_column(conn, "items", "notes TEXT")

        columns = [row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()]
        assert columns == ["id", "notes"]
    finally:
        conn.close()
