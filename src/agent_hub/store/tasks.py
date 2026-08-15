from __future__ import annotations

import time
import uuid
from typing import Any

from ..domain import (
    RunStatus,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
    TaskSubmission,
    TaskUpdate,
)
from .base import (
    _json,
    _decode,
)

class TaskStore:
    """Task lifecycle, claims, leases and controls."""

    def create_task(
        self, item: TaskSubmission, *, tenant_id: str = "default"
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self._condition, self._connection:
            principal = self._principal(item.principal_id)
            delegator = self._actor(item.delegator_actor_id)
            if principal["tenant_id"] != tenant_id:
                raise PermissionError("principal does not belong to tenant")
            if delegator["tenant_id"] != tenant_id:
                raise PermissionError("delegator actor does not belong to tenant")
            if item.assignee_actor_id is not None:
                assignee = self._actor(item.assignee_actor_id)
                if assignee["tenant_id"] != tenant_id:
                    raise PermissionError("assignee actor does not belong to tenant")
            if item.idempotency_key is not None:
                existing = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE principal_id=? AND idempotency_key=? AND tenant_id=?
                    """,
                    (item.principal_id, item.idempotency_key, tenant_id),
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
                    tenant_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    tenant_id,
                    now,
                    now,
                ),
            )
            self._event(
                task_id,
                "task.submitted",
                actor_id=item.delegator_actor_id,
                payload={"origin": item.origin},
                tenant_id=tenant_id,
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id), True

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
            return task

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        tenant_id: str | None = None,
        principal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._lock:
            if principal_id is not None:
                params: list[Any] = [principal_id]
                where = ["principal_id=?"]
                if status is not None:
                    TaskStatus(status)
                    where.append("status=?")
                    params.append(status)
                if tenant_id is not None:
                    where.append("tenant_id=?")
                    params.append(tenant_id)
                params.append(limit)
                rows = self._connection.execute(
                    f"""
                    SELECT task_id FROM hub_tasks
                    WHERE {" AND ".join(where)} ORDER BY created_at DESC LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            elif status is None and tenant_id is None:
                rows = self._connection.execute(
                    "SELECT task_id FROM hub_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif status is None:
                rows = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
            elif tenant_id is None:
                TaskStatus(status)
                rows = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE status=? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                TaskStatus(status)
                rows = self._connection.execute(
                    """
                    SELECT task_id FROM hub_tasks
                    WHERE status=? AND tenant_id=? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, tenant_id, limit),
                ).fetchall()
            return [self._task(str(row["task_id"])) for row in rows]

    def claim_task(
        self,
        *,
        actor_id: str,
        node_id: str,
        wait_seconds: float = 0,
        lease_seconds: float = 120,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        wait_seconds = min(max(wait_seconds, 0), 30)
        lease_seconds = min(max(lease_seconds, 10), 900)
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            with self._connection:
                self._mark_stale_nodes_offline()
            self._validate_executor(actor_id, node_id)
            if tenant_id is not None:
                actor = self._actor(actor_id)
                node = self._node(node_id)
                if actor["tenant_id"] != tenant_id or node["tenant_id"] != tenant_id:
                    raise PermissionError("executor does not belong to tenant")
            while True:
                if self._postgres:
                    with self._connection:
                        self._connection.execute(
                            "SELECT pg_advisory_xact_lock(hashtext('agent_society_task_claim'))"
                        )
                        claimed = self._claim_available(
                            actor_id, node_id, lease_seconds, tenant_id
                        )
                else:
                    claimed = self._claim_available(
                        actor_id, node_id, lease_seconds, tenant_id
                    )
                if claimed is not None:
                    return claimed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def _claim_available(
        self,
        actor_id: str,
        node_id: str,
        lease_seconds: float,
        tenant_id: str | None,
    ) -> dict[str, Any] | None:
        now = time.time()
        actor_capabilities = set(self._actor(actor_id)["capabilities"])
        actor_capabilities.update(self._node(node_id)["capabilities"])
        if tenant_id is None:
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
        else:
            rows = self._connection.execute(
                """
                SELECT task_id, required_capabilities_json, status
                FROM hub_tasks
                WHERE tenant_id=?
                  AND (
                    status=? OR (status=? AND lease_until <= ?)
                  ) AND (assignee_actor_id IS NULL OR assignee_actor_id=?)
                ORDER BY created_at, task_id
                LIMIT 100
                """,
                (
                    tenant_id,
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
                    objective, status, metadata_json, tenant_id, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'remote_task', ?, ?, '{}', ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    task["principal_id"],
                    actor_id,
                    node_id,
                    task["objective"],
                    RunStatus.ACTIVE.value,
                    task["tenant_id"],
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
                tenant_id=task["tenant_id"],
                now=now,
            )
        return {
            "task": self._task(task_id),
            "run": self._run(run_id),
            "lease_token": lease_token,
        }

    def update_task(
        self, task_id: str, item: TaskUpdate, *, tenant_id: str | None = None
    ) -> dict[str, Any]:
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
            task_row = self._task(task_id)
            if tenant_id is not None:
                if task_row["tenant_id"] != tenant_id:
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
                tenant_id=task_row["tenant_id"],
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id)

    def cancel_task(
        self,
        task_id: str,
        *,
        actor_id: str,
        reason: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
            status = TaskStatus(task["status"])
            if status in TERMINAL_TASK_STATUSES:
                return task
            actor = self._actor(actor_id)
            if tenant_id is not None and actor["tenant_id"] != tenant_id:
                raise PermissionError("actor does not belong to tenant")
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
                tenant_id=task["tenant_id"],
                now=now,
            )
            self._condition.notify_all()
            return self._task(task_id)

    def list_task_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        after_seq = max(after_seq, 0)
        limit = min(max(limit, 1), 500)
        with self._lock:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
            rows = self._connection.execute(
                """
                SELECT * FROM hub_task_events
                WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?
                """,
                (task_id, after_seq, limit),
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def create_task_control(
        self,
        task_id: str,
        *,
        actor_id: str,
        kind: str,
        message: str,
        tenant_id: str | None = None,
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
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
            if TaskStatus(task["status"]) in TERMINAL_TASK_STATUSES:
                raise ValueError("task is already terminal")
            actor = self._actor(actor_id)
            if tenant_id is not None and actor["tenant_id"] != tenant_id:
                raise PermissionError("actor does not belong to tenant")
            self._connection.execute(
                """
                INSERT INTO hub_task_controls(
                    control_id, task_id, kind, message, actor_id, tenant_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (control_id, task_id, kind, message, actor_id, task["tenant_id"], now),
            )
            self._event(
                task_id,
                f"task.control.{kind}",
                actor_id=actor_id,
                message=message,
                payload={"control_id": control_id},
                tenant_id=task["tenant_id"],
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
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
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
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
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
                tenant_id=task["tenant_id"],
                now=now,
            )
            return self._control(control_id)

    def mark_task_control_unsupported(
        self,
        task_id: str,
        control_id: str,
        *,
        run_id: str,
        lease_token: str,
        reason: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a leased control when the executing runtime cannot apply it."""
        now = time.time()
        with self._condition, self._connection:
            task = self._task(task_id)
            if tenant_id is not None and task["tenant_id"] != tenant_id:
                raise LookupError("task not found")
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
            if str(row["status"]) in {"delivered", "unsupported"}:
                return self._control(control_id)
            if str(row["status"]) != "leased" or float(row["lease_until"]) < now:
                raise PermissionError("task control lease expired")
            self._connection.execute(
                """
                UPDATE hub_task_controls
                SET status='unsupported', lease_until=0, delivered_at=?
                WHERE control_id=?
                """,
                (now, control_id),
            )
            self._event(
                task_id,
                "task.control.unsupported",
                run_id=run_id,
                payload={
                    "control_id": control_id,
                    "kind": str(row["kind"]),
                    "reason": reason[:10_000],
                },
                tenant_id=task["tenant_id"],
                now=now,
            )
            return self._control(control_id)
