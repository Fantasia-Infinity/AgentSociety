from __future__ import annotations

import time
import uuid
from typing import Any

from ..domain import (
    RunStatus,
    RunSubmission,
    TERMINAL_RUN_STATUSES,
)
from .base import (
    _json,
)

class RunStore:
    """Run lifecycle."""

    def start_run(
        self, item: RunSubmission, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        run_id = f"run_{uuid.uuid4().hex}"
        with self._condition, self._connection:
            principal = self._principal(item.principal_id)
            actor = self._actor(item.actor_id)
            node = self._node(item.node_id)
            if principal["tenant_id"] != tenant_id:
                raise PermissionError("principal does not belong to tenant")
            if actor["principal_id"] != item.principal_id:
                raise ValueError("actor does not belong to principal")
            if node["actor_id"] != item.actor_id:
                raise ValueError("node does not belong to actor")
            if actor["tenant_id"] != tenant_id or node["tenant_id"] != tenant_id:
                raise PermissionError("actor or node does not belong to tenant")
            if item.task_id is not None:
                task = self._task(item.task_id)
                if task["tenant_id"] != tenant_id:
                    raise PermissionError("task does not belong to tenant")
            self._connection.execute(
                """
                INSERT INTO hub_runs(
                    run_id, task_id, principal_id, actor_id, node_id, origin,
                    objective, status, metadata_json, tenant_id, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    tenant_id,
                    now,
                    now,
                ),
            )
            return self._run(run_id)

    def get_run(self, run_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            run = self._run(run_id)
            if tenant_id is not None and run["tenant_id"] != tenant_id:
                raise LookupError("run not found")
            return run

    def list_runs(
        self,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        principal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._lock:
            if principal_id is not None:
                if tenant_id is None:
                    rows = self._connection.execute(
                        """
                        SELECT run_id FROM hub_runs
                        WHERE principal_id=? ORDER BY started_at DESC LIMIT ?
                        """,
                        (principal_id, limit),
                    ).fetchall()
                else:
                    rows = self._connection.execute(
                        """
                        SELECT run_id FROM hub_runs
                        WHERE principal_id=? AND tenant_id=?
                        ORDER BY started_at DESC LIMIT ?
                        """,
                        (principal_id, tenant_id, limit),
                    ).fetchall()
            elif tenant_id is None:
                rows = self._connection.execute(
                    "SELECT run_id FROM hub_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT run_id FROM hub_runs
                    WHERE tenant_id=? ORDER BY started_at DESC LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
            return [self._run(str(row["run_id"])) for row in rows]

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: dict[str, Any],
        error: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            run = self._run(run_id)
            if tenant_id is not None and run["tenant_id"] != tenant_id:
                raise LookupError("run not found")
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
