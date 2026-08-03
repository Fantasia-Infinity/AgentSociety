from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import Condition, RLock
import time
import uuid
from typing import Any

from .domain import (
    ActorRegistration,
    ArtifactSubmission,
    NodeRegistration,
    PrincipalRegistration,
    RunStatus,
    RunSubmission,
    TERMINAL_RUN_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
    TaskSubmission,
    TaskUpdate,
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


class AgentHubStore:
    """Durable local-first coordination state shared by Agent Hosts.

    The store deliberately contains no model-specific behavior. Pi is the first
    runtime adapter, while actors, tasks, runs, and artifacts remain portable.
    """

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
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hub_nodes (
                    node_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL REFERENCES hub_actors(actor_id),
                    display_name TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
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
            self._connection.execute(
                """
                INSERT INTO hub_schema_migrations(version, name, applied_at)
                VALUES (2, 'task_leases_controls_storage', ?)
                ON CONFLICT(version) DO NOTHING
                """,
                (time.time(),),
            )

    def register_principal(self, item: PrincipalRegistration) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._connection.execute(
                """
                INSERT INTO hub_principals(
                    principal_id, kind, display_name, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(principal_id) DO UPDATE SET
                    kind=excluded.kind,
                    display_name=excluded.display_name,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.principal_id,
                    item.kind,
                    item.display_name,
                    _json(item.metadata),
                    now,
                    now,
                ),
            )
            return self._principal(item.principal_id)

    def register_actor(self, item: ActorRegistration) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._require("hub_principals", "principal_id", item.principal_id)
            self._connection.execute(
                """
                INSERT INTO hub_actors(
                    actor_id, principal_id, kind, display_name,
                    capabilities_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    principal_id=excluded.principal_id,
                    kind=excluded.kind,
                    display_name=excluded.display_name,
                    capabilities_json=excluded.capabilities_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.actor_id,
                    item.principal_id,
                    item.kind,
                    item.display_name,
                    _json(item.capabilities),
                    _json(item.metadata),
                    now,
                    now,
                ),
            )
            return self._actor(item.actor_id)

    def register_node(self, item: NodeRegistration) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._require("hub_actors", "actor_id", item.actor_id)
            self._connection.execute(
                """
                INSERT INTO hub_nodes(
                    node_id, actor_id, display_name, capabilities_json,
                    metadata_json, status, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'online', ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    actor_id=excluded.actor_id,
                    display_name=excluded.display_name,
                    capabilities_json=excluded.capabilities_json,
                    metadata_json=excluded.metadata_json,
                    status='online',
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    item.node_id,
                    item.actor_id,
                    item.display_name,
                    _json(item.capabilities),
                    _json(item.metadata),
                    now,
                    now,
                    now,
                ),
            )
            self._condition.notify_all()
            return self._node(item.node_id)

    def heartbeat_node(self, node_id: str) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE hub_nodes
                SET status='online', last_seen_at=?, updated_at=?
                WHERE node_id=?
                """,
                (now, now, node_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("node not found")
            return self._node(node_id)

    def list_principals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT principal_id FROM hub_principals ORDER BY principal_id"
            ).fetchall()
            return [self._principal(str(row["principal_id"])) for row in rows]

    def list_actors(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT actor_id FROM hub_actors ORDER BY actor_id"
            ).fetchall()
            return [self._actor(str(row["actor_id"])) for row in rows]

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock, self._connection:
            self._mark_stale_nodes_offline()
            rows = self._connection.execute(
                "SELECT node_id FROM hub_nodes ORDER BY node_id"
            ).fetchall()
            return [self._node(str(row["node_id"])) for row in rows]

    def create_task(self, item: TaskSubmission) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self._condition, self._connection:
            self._require("hub_principals", "principal_id", item.principal_id)
            self._require("hub_actors", "actor_id", item.delegator_actor_id)
            if item.assignee_actor_id is not None:
                self._require("hub_actors", "actor_id", item.assignee_actor_id)
            if item.idempotency_key is not None:
                existing = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE principal_id=? AND idempotency_key=?
                    """,
                    (item.principal_id, item.idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._task(str(existing["task_id"])), False

            task_id = f"task_{uuid.uuid4().hex}"
            self._connection.execute(
                """
                INSERT INTO hub_tasks(
                    task_id, context_id, principal_id, delegator_actor_id,
                    assignee_actor_id, objective, required_capabilities_json,
                    input_json, metadata_json, origin, status, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    item.context_id,
                    item.principal_id,
                    item.delegator_actor_id,
                    item.assignee_actor_id,
                    item.objective,
                    _json(item.required_capabilities),
                    _json(item.input),
                    _json(item.metadata),
                    item.origin,
                    TaskStatus.SUBMITTED.value,
                    item.idempotency_key,
                    now,
                    now,
                ),
            )
            self._event(
                task_id,
                "task.submitted",
                actor_id=item.delegator_actor_id,
                payload={"origin": item.origin},
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id), True

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            return self._task(task_id)

    def list_tasks(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._lock:
            if status is None:
                rows = self._connection.execute(
                    "SELECT task_id FROM hub_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                TaskStatus(status)
                rows = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE status=? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            return [self._task(str(row["task_id"])) for row in rows]

    def claim_task(
        self,
        *,
        actor_id: str,
        node_id: str,
        wait_seconds: float = 0,
        lease_seconds: float = 120,
    ) -> dict[str, Any] | None:
        wait_seconds = min(max(wait_seconds, 0), 30)
        lease_seconds = min(max(lease_seconds, 10), 900)
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            with self._connection:
                self._mark_stale_nodes_offline()
            self._validate_executor(actor_id, node_id)
            while True:
                if self._postgres:
                    with self._connection:
                        self._connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtext('agent_society_task_claim'))"
                        )
                        claimed = self._claim_available(
                            actor_id, node_id, lease_seconds
                        )
                else:
                    claimed = self._claim_available(
                        actor_id, node_id, lease_seconds
                    )
                if claimed is not None:
                    return claimed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def _claim_available(
        self, actor_id: str, node_id: str, lease_seconds: float
    ) -> dict[str, Any] | None:
        now = time.time()
        actor_capabilities = set(self._actor(actor_id)["capabilities"])
        actor_capabilities.update(self._node(node_id)["capabilities"])
        rows = self._connection.execute(
            """
            SELECT task_id, required_capabilities_json, status
            FROM hub_tasks
            WHERE (
                status=? OR (status=? AND lease_until <= ?)
            ) AND (assignee_actor_id IS NULL OR assignee_actor_id=?)
            ORDER BY created_at, task_id
            LIMIT 100
            """,
            (
                TaskStatus.SUBMITTED.value,
                TaskStatus.WORKING.value,
                now,
                actor_id,
            ),
        ).fetchall()
        selected: Any | None = None
        for row in rows:
            required = set(_decode(str(row["required_capabilities_json"])))
            if required.issubset(actor_capabilities):
                selected = row
                break
        if selected is None:
            return None

        task_id = str(selected["task_id"])
        if str(selected["status"]) == TaskStatus.WORKING.value:
            self._connection.execute(
                """
                UPDATE hub_runs
                SET status=?, error='lease_expired', updated_at=?, completed_at=?
                WHERE task_id=? AND status=?
                """,
                (
                    RunStatus.FAILED.value,
                    now,
                    now,
                    task_id,
                    RunStatus.ACTIVE.value,
                ),
            )

        run_id = f"run_{uuid.uuid4().hex}"
        lease_token = uuid.uuid4().hex + uuid.uuid4().hex
        task = self._task(task_id)
        with self._connection:
            self._connection.execute(
                """
                UPDATE hub_tasks
                SET status=?, executor_actor_id=?, executor_node_id=?, lease_token=?,
                    lease_until=?, lease_seconds=?, attempts=attempts+1,
                    error=NULL, updated_at=?
                WHERE task_id=?
                """,
                (
                    TaskStatus.WORKING.value,
                    actor_id,
                    node_id,
                    lease_token,
                    now + lease_seconds,
                    lease_seconds,
                    now,
                    task_id,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO hub_runs(
                    run_id, task_id, principal_id, actor_id, node_id, origin,
                    objective, status, metadata_json, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'remote_task', ?, ?, '{}', ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    task["principal_id"],
                    actor_id,
                    node_id,
                    task["objective"],
                    RunStatus.ACTIVE.value,
                    now,
                    now,
                ),
            )
            self._event(
                task_id,
                "task.claimed",
                run_id=run_id,
                actor_id=actor_id,
                node_id=node_id,
                payload={"lease_until": now + lease_seconds},
                now=now,
            )
        return {
            "task": self._task(task_id),
            "run": self._run(run_id),
            "lease_token": lease_token,
        }

    def update_task(self, task_id: str, item: TaskUpdate) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            row = self._connection.execute(
                """
                SELECT status, lease_token, lease_until, lease_seconds,
                       executor_actor_id, executor_node_id
                FROM hub_tasks WHERE task_id=?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise LookupError("task not found")
            current = TaskStatus(str(row["status"]))
            if current in TERMINAL_TASK_STATUSES:
                raise ValueError("task is already terminal")
            if str(row["lease_token"] or "") != item.lease_token:
                raise PermissionError("invalid task lease")
            if float(row["lease_until"]) < now:
                raise PermissionError("task lease expired")
            run = self._run(item.run_id)
            if run["task_id"] != task_id or run["status"] != RunStatus.ACTIVE.value:
                raise ValueError("run is not active for this task")

            completed_at = now if item.status in TERMINAL_TASK_STATUSES else None
            lease_until = (
                0 if completed_at is not None else now + float(row["lease_seconds"])
            )
            error = item.message if item.status == TaskStatus.FAILED else None
            self._connection.execute(
                """
                UPDATE hub_tasks
                SET status=?, result_json=?, error=?, lease_until=?,
                    updated_at=?, completed_at=?
                WHERE task_id=?
                """,
                (
                    item.status.value,
                    _json(item.result),
                    error,
                    lease_until,
                    now,
                    completed_at,
                    task_id,
                ),
            )
            if completed_at is not None:
                run_status = RunStatus(item.status.value)
                self._connection.execute(
                    """
                    UPDATE hub_runs
                    SET status=?, result_json=?, error=?, updated_at=?, completed_at=?
                    WHERE run_id=?
                    """,
                    (
                        run_status.value,
                        _json(item.result),
                        error,
                        now,
                        now,
                        item.run_id,
                    ),
                )
            self._event(
                task_id,
                f"task.{item.status.value}",
                run_id=item.run_id,
                actor_id=str(row["executor_actor_id"]),
                node_id=str(row["executor_node_id"]),
                message=item.message,
                payload={"result": item.result},
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id)

    def cancel_task(self, task_id: str, *, actor_id: str, reason: str | None) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            status = TaskStatus(task["status"])
            if status in TERMINAL_TASK_STATUSES:
                return task
            self._require("hub_actors", "actor_id", actor_id)
            self._connection.execute(
                """
                UPDATE hub_tasks
                SET status=?, error=?, lease_until=0, updated_at=?, completed_at=?
                WHERE task_id=?
                """,
                (TaskStatus.CANCELLED.value, reason, now, now, task_id),
            )
            self._connection.execute(
                """
                UPDATE hub_runs
                SET status=?, error=?, updated_at=?, completed_at=?
                WHERE task_id=? AND status=?
                """,
                (
                    RunStatus.CANCELLED.value,
                    reason,
                    now,
                    now,
                    task_id,
                    RunStatus.ACTIVE.value,
                ),
            )
            self._event(
                task_id,
                "task.cancelled",
                actor_id=actor_id,
                message=reason,
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id)

    def list_task_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        after_seq = max(after_seq, 0)
        limit = min(max(limit, 1), 500)
        with self._lock:
            self._task(task_id)
            rows = self._connection.execute(
                """
                SELECT * FROM hub_task_events
                WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?
                """,
                (task_id, after_seq, limit),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def create_task_control(
        self, task_id: str, *, actor_id: str, kind: str, message: str
    ) -> dict[str, Any]:
        if kind not in {"steer", "follow_up"}:
            raise ValueError("control kind must be steer or follow_up")
        message = message.strip()
        if not message:
            raise ValueError("control message is required")
        if len(message) > 50_000:
            raise ValueError("control message exceeds 50000 characters")
        now = time.time()
        control_id = f"control_{uuid.uuid4().hex}"
        with self._condition, self._connection:
            task = self._task(task_id)
            if TaskStatus(task["status"]) in TERMINAL_TASK_STATUSES:
                raise ValueError("task is already terminal")
            self._require("hub_actors", "actor_id", actor_id)
            self._connection.execute(
                """
                INSERT INTO hub_task_controls(
                    control_id, task_id, kind, message, actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (control_id, task_id, kind, message, actor_id, now),
            )
            self._event(
                task_id,
                f"task.control.{kind}",
                actor_id=actor_id,
                message=message,
                payload={"control_id": control_id},
                now=now,
            )
            self._condition.notify_all()
            return self._control(control_id)

    def claim_task_controls(
        self,
        task_id: str,
        *,
        run_id: str,
        lease_token: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            if task["status"] != TaskStatus.WORKING.value:
                return []
            row = self._connection.execute(
                "SELECT lease_token, lease_until FROM hub_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None or str(row["lease_token"] or "") != lease_token:
                raise PermissionError("invalid task lease")
            if float(row["lease_until"]) < now:
                raise PermissionError("task lease expired")
            run = self._run(run_id)
            if run["task_id"] != task_id or run["status"] != RunStatus.ACTIVE.value:
                raise ValueError("run is not active for this task")
            rows = self._connection.execute(
                """
                SELECT control_id FROM hub_task_controls
                WHERE task_id=? AND (
                    status='pending' OR (status='leased' AND lease_until<=?)
                ) ORDER BY seq LIMIT ?
                """,
                (task_id, now, limit),
            ).fetchall()
            controls: list[dict[str, Any]] = []
            for row in rows:
                control_id = str(row["control_id"])
                control_lease = uuid.uuid4().hex + uuid.uuid4().hex
                self._connection.execute(
                    """
                    UPDATE hub_task_controls
                    SET status='leased', run_id=?, lease_token=?, lease_until=?
                    WHERE control_id=?
                    """,
                    (run_id, control_lease, now + 30, control_id),
                )
                control = self._control(control_id)
                control["lease_token"] = control_lease
                controls.append(control)
            return controls

    def acknowledge_task_control(
        self,
        task_id: str,
        control_id: str,
        *,
        run_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            row = self._connection.execute(
                "SELECT * FROM hub_task_controls WHERE control_id=? AND task_id=?",
                (control_id, task_id),
            ).fetchone()
            if row is None:
                raise LookupError("task control not found")
            if str(row["run_id"] or "") != run_id:
                raise PermissionError("task control belongs to another run")
            if str(row["lease_token"] or "") != lease_token:
                raise PermissionError("invalid task control lease")
            if str(row["status"]) == "delivered":
                return self._control(control_id)
            if str(row["status"]) != "leased" or float(row["lease_until"]) < now:
                raise PermissionError("task control lease expired")
            self._connection.execute(
                """
                UPDATE hub_task_controls
                SET status='delivered', lease_until=0, delivered_at=?
                WHERE control_id=?
                """,
                (now, control_id),
            )
            self._event(
                task_id,
                "task.control.delivered",
                run_id=run_id,
                payload={"control_id": control_id, "kind": str(row["kind"])},
                now=now,
            )
            return self._control(control_id)

    def start_run(self, item: RunSubmission) -> dict[str, Any]:
        now = time.time()
        run_id = f"run_{uuid.uuid4().hex}"
        with self._condition, self._connection:
            self._require("hub_principals", "principal_id", item.principal_id)
            actor = self._actor(item.actor_id)
            node = self._node(item.node_id)
            if actor["principal_id"] != item.principal_id:
                raise ValueError("actor does not belong to principal")
            if node["actor_id"] != item.actor_id:
                raise ValueError("node does not belong to actor")
            if item.task_id is not None:
                self._task(item.task_id)
            self._connection.execute(
                """
                INSERT INTO hub_runs(
                    run_id, task_id, principal_id, actor_id, node_id, origin,
                    objective, status, metadata_json, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.task_id,
                    item.principal_id,
                    item.actor_id,
                    item.node_id,
                    item.origin,
                    item.objective,
                    RunStatus.ACTIVE.value,
                    _json(item.metadata),
                    now,
                    now,
                ),
            )
            return self._run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._run(run_id)

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: dict[str, Any],
        error: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            run = self._run(run_id)
            current = RunStatus(run["status"])
            if current in TERMINAL_RUN_STATUSES:
                raise ValueError("run is already terminal")
            completed_at = now if status in TERMINAL_RUN_STATUSES else None
            self._connection.execute(
                """
                UPDATE hub_runs
                SET status=?, result_json=?, error=?, updated_at=?, completed_at=?
                WHERE run_id=?
                """,
                (status.value, _json(result), error, now, completed_at, run_id),
            )
            return self._run(run_id)

    def add_artifact(self, item: ArtifactSubmission) -> dict[str, Any]:
        now = time.time()
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self._condition, self._connection:
            self._require("hub_actors", "actor_id", item.created_by_actor_id)
            if item.task_id is not None:
                self._task(item.task_id)
            if item.run_id is not None:
                run = self._run(item.run_id)
                if item.task_id is not None and run["task_id"] != item.task_id:
                    raise ValueError("artifact task_id and run_id do not match")
            self._connection.execute(
                """
                INSERT INTO hub_artifacts(
                    artifact_id, task_id, run_id, name, media_type, uri, sha256,
                    size_bytes, created_by_actor_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    item.task_id,
                    item.run_id,
                    item.name,
                    item.media_type,
                    item.uri,
                    item.sha256,
                    item.size_bytes,
                    item.created_by_actor_id,
                    _json(item.metadata),
                    now,
                ),
            )
            if item.task_id is not None:
                self._event(
                    item.task_id,
                    "artifact.created",
                    run_id=item.run_id,
                    actor_id=item.created_by_actor_id,
                    payload={"artifact_id": artifact_id, "name": item.name},
                    now=now,
                )
            return self._artifact(artifact_id)

    def stats(self) -> dict[str, int]:
        with self._lock, self._connection:
            self._mark_stale_nodes_offline()
            result: dict[str, int] = {}
            for name, table in (
                ("principals", "hub_principals"),
                ("actors", "hub_actors"),
                ("nodes", "hub_nodes"),
                ("tasks", "hub_tasks"),
                ("runs", "hub_runs"),
                ("artifacts", "hub_artifacts"),
            ):
                row = self._connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()
                result[name] = int(row["count"]) if row is not None else 0
            return result

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
        now: float | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO hub_task_events(
                event_id, task_id, run_id, event_type, actor_id, node_id,
                message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "created_at": float(row["created_at"]),
        }
