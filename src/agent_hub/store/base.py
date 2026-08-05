from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import Condition, RLock
import time
import uuid
from typing import Any

TENANT_TABLES = (
    "hub_principals",
    "hub_actors",
    "hub_nodes",
    "hub_tasks",
    "hub_runs",
    "hub_task_events",
    "hub_artifacts",
    "hub_task_controls",
    "hub_user_accounts",
    "hub_auth_sessions",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str) -> Any:
    return json.loads(value)


class _PostgresConnection:
    """Small DB-API compatibility layer used by the existing store queries."""

    def __init__(self, url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL requires the 'postgres' optional dependency"
            ) from exc
        self._connection = psycopg.connect(
            url, row_factory=dict_row, autocommit=True
        )
        self._transactions: list[Any] = []

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()):
        return self._connection.execute(sql.replace("?", "%s"), parameters)

    def executescript(self, sql: str) -> None:
        postgres_sql = sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
        )
        for statement in postgres_sql.split(";"):
            if statement.strip():
                self._connection.execute(statement)

    def __enter__(self) -> "_PostgresConnection":
        transaction = self._connection.transaction()
        transaction.__enter__()
        self._transactions.append(transaction)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        transaction = self._transactions.pop()
        return transaction.__exit__(exc_type, exc, traceback)

    def close(self) -> None:
        self._connection.close()




class StoreBase:
    """SQLite/PostgreSQL connection, schema and shared row helpers."""

    def __init__(
        self, path: Path | str, *, node_stale_seconds: float = 90
    ) -> None:
        raw_path = str(path)
        self._postgres = raw_path.startswith(("postgres://", "postgresql://"))
        if self._postgres:
            self._connection: Any = _PostgresConnection(raw_path)
        else:
            sqlite_path = Path(path)
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(sqlite_path), check_same_thread=False, timeout=30
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._node_stale_seconds = min(max(node_stale_seconds, 15), 3600)
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS hub_principals (
                    principal_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_actors (
                    actor_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL REFERENCES hub_principals(principal_id),
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_nodes (
                    node_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    display_name TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL DEFAULT 'online',
                    last_seen_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_tasks (
                    task_id TEXT PRIMARY KEY,
                    context_id TEXT,
                    principal_id TEXT NOT NULL REFERENCES hub_principals(principal_id),
                    delegator_actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    assignee_actor_id TEXT REFERENCES hub_actors(actor_id),
                    executor_actor_id TEXT REFERENCES hub_actors(actor_id),
                    executor_node_id TEXT REFERENCES hub_nodes(node_id),
                    objective TEXT NOT NULL,
                    required_capabilities_json TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    lease_seconds REAL NOT NULL DEFAULT 120,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_tasks_idempotency
                ON hub_tasks(principal_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_hub_tasks_claim
                ON hub_tasks(status, assignee_actor_id, lease_until, created_at);

                CREATE TABLE IF NOT EXISTS hub_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES hub_tasks(task_id),
                    principal_id TEXT NOT NULL REFERENCES hub_principals(principal_id),
                    actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    node_id TEXT NOT NULL REFERENCES hub_nodes(node_id),
                    origin TEXT NOT NULL,
                    objective TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_hub_runs_task
                ON hub_runs(task_id, started_at);

                CREATE TABLE IF NOT EXISTS hub_task_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    task_id TEXT NOT NULL REFERENCES hub_tasks(task_id),
                    run_id TEXT REFERENCES hub_runs(run_id),
                    event_type TEXT NOT NULL,
                    actor_id TEXT REFERENCES hub_actors(actor_id),
                    node_id TEXT REFERENCES hub_nodes(node_id),
                    message TEXT,
                    payload_json TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES hub_tasks(task_id),
                    run_id TEXT REFERENCES hub_runs(run_id),
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    created_by_actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    metadata_json TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_hub_artifacts_task
                ON hub_artifacts(task_id, created_at);

                CREATE TABLE IF NOT EXISTS hub_task_controls (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    control_id TEXT UNIQUE NOT NULL,
                    task_id TEXT NOT NULL REFERENCES hub_tasks(task_id),
                    run_id TEXT REFERENCES hub_runs(run_id),
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at REAL NOT NULL,
                    delivered_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_hub_task_controls_claim
                ON hub_task_controls(task_id, status, lease_until, seq);

                CREATE TABLE IF NOT EXISTS hub_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_tenants (
                    tenant_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_auth_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    tenant_id TEXT NOT NULL REFERENCES hub_tenants(tenant_id),
                    role TEXT NOT NULL,
                    principal_id TEXT,
                    actor_id TEXT,
                    node_id TEXT,
                    label TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS hub_oidc_identities (
                    provider TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    tenant_id TEXT NOT NULL REFERENCES hub_tenants(tenant_id),
                    principal_id TEXT NOT NULL REFERENCES hub_principals(principal_id),
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (provider, subject)
                );

                CREATE TABLE IF NOT EXISTS hub_user_accounts (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    principal_id TEXT UNIQUE NOT NULL REFERENCES hub_principals(principal_id),
                    tenant_id TEXT NOT NULL REFERENCES hub_tenants(tenant_id),
                    role TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_auth_sessions (
                    session_token_hash TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL REFERENCES hub_principals(principal_id),
                    tenant_id TEXT NOT NULL REFERENCES hub_tenants(tenant_id),
                    role TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    last_seen_at REAL
                );
                """
            )
            # Databases created by pre-migration versions do not receive new
            # columns from CREATE TABLE IF NOT EXISTS. Keep the migration
            # deliberately additive so existing LAN deployments can upgrade
            # in place without exporting their SQLite state.
            if self._postgres:
                self._connection.execute(
                    "ALTER TABLE hub_tasks ADD COLUMN IF NOT EXISTS lease_seconds REAL NOT NULL DEFAULT 120"
                )
            else:
                columns = {
                    str(row[1])
                    for row in self._connection.execute("PRAGMA table_info(hub_tasks)")
                }
                if "lease_seconds" not in columns:
                    self._connection.execute(
                        "ALTER TABLE hub_tasks ADD COLUMN lease_seconds REAL NOT NULL DEFAULT 120"
                    )
                self._connection.execute("PRAGMA user_version=2")
            now = time.time()
            if self._postgres:
                for table in TENANT_TABLES:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'"
                    )
            else:
                for table in TENANT_TABLES:
                    columns = {
                        str(row[1])
                        for row in self._connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    if "tenant_id" not in columns:
                        self._connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                        )
                self._connection.execute("PRAGMA user_version=3")
            self._connection.execute(
                """
                INSERT INTO hub_tenants(tenant_id, display_name, metadata_json, created_at, updated_at)
                VALUES ('default', 'Default', '{}', ?, ?)
                ON CONFLICT(tenant_id) DO NOTHING
                """,
                (now, now),
            )
            for table in TENANT_TABLES:
                self._connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)"
                )
            self._connection.execute(
                """
                INSERT INTO hub_schema_migrations(version, name, applied_at)
                VALUES (2, 'task_leases_controls_storage', ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (now,),
            )
            self._connection.execute(
                """
                INSERT INTO hub_schema_migrations(version, name, applied_at)
                VALUES (3, 'multi_tenant_tokens_oidc', ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (now,),
            )

    def _tenant(self, tenant_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_tenants WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise LookupError("tenant not found")
        return {
            "tenant_id": str(row["tenant_id"]),
            "display_name": str(row["display_name"]),
            "metadata": _decode(str(row["metadata_json"])),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _token_record(self, token_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_auth_tokens WHERE token_id=?", (token_id,)
        ).fetchone()
        if row is None:
            raise LookupError("token not found")
        return {
            "token_id": str(row["token_id"]),
            "tenant_id": str(row["tenant_id"]),
            "role": str(row["role"]),
            "principal_id": row["principal_id"],
            "actor_id": row["actor_id"],
            "node_id": row["node_id"],
            "label": str(row["label"]),
            "created_at": float(row["created_at"]),
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass

    def _validate_executor(self, actor_id: str, node_id: str) -> None:
        actor = self._actor(actor_id)
        node = self._node(node_id)
        if actor["kind"] != "agent":
            raise ValueError("only agent actors can claim tasks")
        if node["actor_id"] != actor_id:
            raise ValueError("node does not belong to actor")
        if node["status"] != "online":
            raise ValueError("node is offline; heartbeat before claiming tasks")

    def _mark_stale_nodes_offline(self) -> None:
        now = time.time()
        self._connection.execute(
            """
            UPDATE hub_nodes
            SET status='offline', updated_at=?
            WHERE status='online' AND last_seen_at<?
            """,
            (now, now - self._node_stale_seconds),
        )

    def _require(self, table: str, key: str, value: str) -> None:
        row = self._connection.execute(
            f"SELECT 1 FROM {table} WHERE {key}=?", (value,)
        ).fetchone()
        if row is None:
            label = table.removeprefix("hub_").removesuffix("s")
            raise LookupError(f"{label} not found")

    def _principal(self, principal_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_principals WHERE principal_id=?", (principal_id,)
        ).fetchone()
        if row is None:
            raise LookupError("principal not found")
        return {
            "principal_id": str(row["principal_id"]),
            "kind": str(row["kind"]),
            "display_name": str(row["display_name"]),
            "metadata": _decode(str(row["metadata_json"])),
            "tenant_id": str(row["tenant_id"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _actor(self, actor_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_actors WHERE actor_id=?", (actor_id,)
        ).fetchone()
        if row is None:
            raise LookupError("actor not found")
        return {
            "actor_id": str(row["actor_id"]),
            "principal_id": str(row["principal_id"]),
            "kind": str(row["kind"]),
            "display_name": str(row["display_name"]),
            "capabilities": _decode(str(row["capabilities_json"])),
            "metadata": _decode(str(row["metadata_json"])),
            "tenant_id": str(row["tenant_id"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _node(self, node_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        if row is None:
            raise LookupError("node not found")
        return {
            "node_id": str(row["node_id"]),
            "actor_id": str(row["actor_id"]),
            "display_name": str(row["display_name"]),
            "capabilities": _decode(str(row["capabilities_json"])),
            "metadata": _decode(str(row["metadata_json"])),
            "tenant_id": str(row["tenant_id"]),
            "status": str(row["status"]),
            "last_seen_at": float(row["last_seen_at"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _task(self, task_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise LookupError("task not found")
        artifacts = self._connection.execute(
            """
            SELECT artifact_id FROM hub_artifacts
            WHERE task_id=? ORDER BY created_at
            """,
            (task_id,),
        ).fetchall()
        return {
            "task_id": str(row["task_id"]),
            "context_id": row["context_id"],
            "principal_id": str(row["principal_id"]),
            "delegator_actor_id": str(row["delegator_actor_id"]),
            "assignee_actor_id": row["assignee_actor_id"],
            "executor_actor_id": row["executor_actor_id"],
            "executor_node_id": row["executor_node_id"],
            "objective": str(row["objective"]),
            "required_capabilities": _decode(
                str(row["required_capabilities_json"])
            ),
            "input": _decode(str(row["input_json"])),
            "metadata": _decode(str(row["metadata_json"])),
            "origin": str(row["origin"]),
            "status": str(row["status"]),
            "result": _decode(str(row["result_json"])),
            "error": row["error"],
            "tenant_id": str(row["tenant_id"]),
            "lease_until": float(row["lease_until"]),
            "lease_seconds": float(row["lease_seconds"]),
            "attempts": int(row["attempts"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "completed_at": row["completed_at"],
            "artifacts": [
                self._artifact(str(item["artifact_id"])) for item in artifacts
            ],
        }

    def _control(self, control_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_task_controls WHERE control_id=?", (control_id,)
        ).fetchone()
        if row is None:
            raise LookupError("task control not found")
        return {
            "seq": int(row["seq"]),
            "control_id": str(row["control_id"]),
            "task_id": str(row["task_id"]),
            "run_id": row["run_id"],
            "kind": str(row["kind"]),
            "message": str(row["message"]),
            "actor_id": str(row["actor_id"]),
            "status": str(row["status"]),
            "tenant_id": str(row["tenant_id"]),
            "lease_until": float(row["lease_until"]),
            "created_at": float(row["created_at"]),
            "delivered_at": row["delivered_at"],
        }

    def _run(self, run_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError("run not found")
        return {
            "run_id": str(row["run_id"]),
            "task_id": row["task_id"],
            "principal_id": str(row["principal_id"]),
            "actor_id": str(row["actor_id"]),
            "node_id": str(row["node_id"]),
            "origin": str(row["origin"]),
            "objective": row["objective"],
            "status": str(row["status"]),
            "metadata": _decode(str(row["metadata_json"])),
            "result": _decode(str(row["result_json"])),
            "error": row["error"],
            "tenant_id": str(row["tenant_id"]),
            "started_at": float(row["started_at"]),
            "updated_at": float(row["updated_at"]),
            "completed_at": row["completed_at"],
        }

    def _artifact(self, artifact_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise LookupError("artifact not found")
        return {
            "artifact_id": str(row["artifact_id"]),
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "name": str(row["name"]),
            "media_type": str(row["media_type"]),
            "uri": str(row["uri"]),
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "created_by_actor_id": str(row["created_by_actor_id"]),
            "metadata": _decode(str(row["metadata_json"])),
            "tenant_id": str(row["tenant_id"]),
            "created_at": float(row["created_at"]),
        }

    def _event(
        self,
        task_id: str,
        event_type: str,
        *,
        run_id: str | None = None,
        actor_id: str | None = None,
        node_id: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        now: float | None = None,
    ) -> None:
        if tenant_id is None:
            row = self._connection.execute(
                "SELECT tenant_id FROM hub_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            tenant_id = str(row["tenant_id"]) if row is not None else "default"
        self._connection.execute(
            """
            INSERT INTO hub_task_events(
                event_id, task_id, run_id, event_type, actor_id, node_id,
                message, payload_json, tenant_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event_{uuid.uuid4().hex}",
                task_id,
                run_id,
                event_type,
                actor_id,
                node_id,
                message,
                _json(payload or {}),
                tenant_id,
                now if now is not None else time.time(),
            ),
        )

    @staticmethod
    def _event_dict(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "event_id": str(row["event_id"]),
            "task_id": str(row["task_id"]),
            "run_id": row["run_id"],
            "type": str(row["event_type"]),
            "actor_id": row["actor_id"],
            "node_id": row["node_id"],
            "message": row["message"],
            "payload": _decode(str(row["payload_json"])),
            "tenant_id": str(row["tenant_id"]),
            "created_at": float(row["created_at"]),
        }
