from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

import psycopg


DEFAULT_DSN = "postgresql://jobbot:jobbot@localhost:5432/jobbot"
TABLE_IMPORT_ORDER = [
    "github_repo_state",
    "board_seeds",
    "source_runs",
    "source_errors",
    "company_watchlist",
    "company_requests",
    "company_scan_targets",
    "company_scan_history",
    "job_user_profiles",
    "job_application_statuses",
    "process_notes",
    "canonical_jobs",
    "job_variants",
    "fingerprints",
    "discord_posts",
    "dedupe_matches",
]
TABLES_WITH_ID_SEQUENCES = [
    "canonical_jobs",
    "job_variants",
    "discord_posts",
    "source_runs",
    "source_errors",
    "dedupe_matches",
    "company_watchlist",
    "company_requests",
    "company_scan_targets",
    "company_scan_history",
    "job_application_statuses",
    "process_notes",
]


def sqlite_table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def sqlite_column_names(connection: sqlite3.Connection, table_name: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [row["name"] for row in rows]


def iter_sqlite_rows(connection: sqlite3.Connection, table_name: str) -> Iterable[dict[str, object]]:
    cursor = connection.execute(f"SELECT * FROM {table_name}")
    for row in cursor.fetchall():
        yield dict(row)


def sync_sequence(connection: psycopg.Connection, table_name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                EXISTS(SELECT 1 FROM {table_name})
            )
            """
        )


def table_has_rows(connection: psycopg.Connection, table_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT EXISTS(SELECT 1 FROM {table_name})")
        row = cursor.fetchone()
    return bool(row and row[0])


def import_table(
    sqlite_connection: sqlite3.Connection,
    postgres_connection: psycopg.Connection,
    table_name: str,
) -> int:
    if not sqlite_table_exists(sqlite_connection, table_name):
        return 0

    columns = sqlite_column_names(sqlite_connection, table_name)
    if not columns:
        return 0

    rows = list(iter_sqlite_rows(sqlite_connection, table_name))
    if not rows:
        return 0

    column_sql = ", ".join(columns)
    placeholder_sql = ", ".join(["%s"] * len(columns))
    insert_sql = (
        f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholder_sql}) "
        "ON CONFLICT DO NOTHING"
    )

    with postgres_connection.cursor() as cursor:
        for row in rows:
            cursor.execute(insert_sql, [row[column] for column in columns])
    return len(rows)


def main() -> None:
    load_dotenv()
    sqlite_path = Path(os.getenv("SQLITE_DB_PATH", "job_bot.db"))
    postgres_dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)

    if not sqlite_path.exists():
        raise SystemExit(f"SQLite source does not exist: {sqlite_path}")

    with sqlite3.connect(sqlite_path) as sqlite_connection, psycopg.connect(postgres_dsn) as postgres_connection:
        sqlite_connection.row_factory = sqlite3.Row

        imported_counts: dict[str, int] = {}
        for table_name in TABLE_IMPORT_ORDER:
            count = import_table(sqlite_connection, postgres_connection, table_name)
            imported_counts[table_name] = count

        for table_name in TABLES_WITH_ID_SEQUENCES:
            if table_has_rows(postgres_connection, table_name):
                sync_sequence(postgres_connection, table_name)

        postgres_connection.commit()

    print("SQLite -> Postgres import complete.")
    for table_name, count in imported_counts.items():
        print(f"{table_name}: {count}")


if __name__ == "__main__":
    main()
