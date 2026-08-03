from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3

from .store import AgentHubStore


TABLES = (
    "hub_principals",
    "hub_actors",
    "hub_nodes",
    "hub_tasks",
    "hub_runs",
    "hub_task_events",
    "hub_artifacts",
    "hub_task_controls",
    "hub_schema_migrations",
)


def migrate_sqlite_to_postgres(source: Path, database_url: str) -> dict[str, int]:
    if not source.is_file():
        raise ValueError(f"SQLite source does not exist: {source}")
    # Ensure the destination schema is current before copying any rows.
    destination_store = AgentHubStore(database_url)
    destination_store.close()
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL migration requires the 'postgres' optional dependency"
        ) from exc

    source_connection = sqlite3.connect(str(source))
    source_connection.row_factory = sqlite3.Row
    destination = psycopg.connect(database_url)
    counts: dict[str, int] = {}
    try:
        with destination.transaction():
            for table in TABLES:
                columns = [
                    str(row[1])
                    for row in source_connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                ]
                if not columns:
                    counts[table] = 0
                    continue
                rows = source_connection.execute(f"SELECT * FROM {table}").fetchall()
                placeholders = ",".join("%s" for _ in columns)
                column_sql = ",".join(columns)
                for row in rows:
                    destination.execute(
                        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                        tuple(row[column] for column in columns),
                    )
                counts[table] = len(rows)
            for table in ("hub_task_events", "hub_task_controls"):
                destination.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'seq'), "
                    f"COALESCE((SELECT MAX(seq) FROM {table}), 1), true)",
                    (table,),
                )
    finally:
        destination.close()
        source_connection.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy an AgentSociety Hub SQLite database to PostgreSQL."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("AGENT_HUB_DATABASE_URL", ""),
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or AGENT_HUB_DATABASE_URL is required")
    counts = migrate_sqlite_to_postgres(args.source, args.database_url)
    for table, count in counts.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()
