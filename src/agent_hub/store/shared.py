from __future__ import annotations

import time
from typing import Any

from ..domain import SharedEventAppend
from .base import _decode, _json


class SharedStore:
    """Principal-scoped shared memory: an append-only, idempotent event log.

    Scope values: 'consensus' (session digests, facts, decisions), 'directory'
    (session directory upserts), 'qa' (question answers). Every entry carries a
    tenant + principal so the existing token isolation model applies verbatim.
    The log is the source of truth; snapshots are derived summaries.
    """

    MAX_SHARED_EVENT_PAYLOAD = 8192

    def append_shared_event(
        self, item: SharedEventAppend, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            principal = self._principal(item.principal_id)
            if principal["tenant_id"] != tenant_id:
                raise PermissionError("principal does not belong to tenant")
            if item.actor_id is not None:
                actor = self._actor(item.actor_id)
                if actor["tenant_id"] != tenant_id:
                    raise PermissionError("actor does not belong to tenant")
                if actor["principal_id"] != item.principal_id:
                    raise ValueError("actor does not belong to principal")
            if item.node_id is not None:
                node = self._node(item.node_id)
                if node["tenant_id"] != tenant_id:
                    raise PermissionError("node does not belong to tenant")
            if item.event_id is not None:
                existing = self._connection.execute(
                    """
                    SELECT seq FROM hub_shared_events
                    WHERE event_id=? AND tenant_id=?
                    """,
                    (item.event_id, tenant_id),
                ).fetchone()
                if existing is not None:
                    return self._shared_event_by_seq(int(existing["seq"]))

            expires_at = (
                now + item.ttl_hours * 3600 if item.ttl_hours is not None else None
            )
            event_id = item.event_id or f"shared_{self._random_id()}"
            self._connection.execute(
                """
                INSERT INTO hub_shared_events(
                    event_id, tenant_id, principal_id, scope, kind, session_id,
                    actor_id, node_id, payload_json, ttl_hours, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    tenant_id,
                    item.principal_id,
                    item.scope,
                    item.kind,
                    item.session_id,
                    item.actor_id,
                    item.node_id,
                    _json(item.payload),
                    item.ttl_hours,
                    expires_at,
                    now,
                ),
            )
            self._condition.notify_all()
            # Backend-neutral seq read: last_insert_rowid() is SQLite-only and
            # PostgreSQL deployments would fail every append.
            row = self._connection.execute(
                """
                SELECT seq FROM hub_shared_events
                WHERE event_id=? AND tenant_id=?
                """,
                (event_id, tenant_id),
            ).fetchone()
            if row is None:
                raise LookupError("shared event not found after insert")
            return self._shared_event_by_seq(int(row["seq"]))

    def list_shared_events(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        after_seq: int = 0,
        scope: str | None = None,
        kind: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        after_seq = max(after_seq, 0)
        limit = min(max(limit, 1), 500)
        now = time.time()
        with self._lock:
            params: list[Any] = [tenant_id, after_seq]
            where = [
                "tenant_id=?",
                "seq>?",
                "(expires_at IS NULL OR expires_at>?)",
            ]
            params.append(now)
            if principal_id is not None:
                where.append("principal_id=?")
                params.append(principal_id)
            if scope is not None:
                where.append("scope=?")
                params.append(scope)
            if kind is not None:
                where.append("kind=?")
                params.append(kind)
            if session_id is not None:
                where.append("session_id=?")
                params.append(session_id)
            params.append(limit)
            rows = self._connection.execute(
                f"""
                SELECT * FROM hub_shared_events
                WHERE {" AND ".join(where)}
                ORDER BY seq{" DESC" if order == "desc" and after_seq <= 0 else ""} LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [self._shared_event_dict(row) for row in rows]

    def max_shared_event_seq(
        self, *, tenant_id: str, principal_id: str | None = None
    ) -> int:
        """Highest seq visible to this tenant/principal (0 when empty)."""
        with self._lock:
            params: list[Any] = [tenant_id]
            where = ["tenant_id=?"]
            if principal_id is not None:
                where.append("principal_id=?")
                params.append(principal_id)
            row = self._connection.execute(
                f"SELECT MAX(seq) AS m FROM hub_shared_events WHERE {' AND '.join(where)}",
                tuple(params),
            ).fetchone()
            return int(row["m"] or 0) if row else 0

    def shared_snapshot(
        self, *, tenant_id: str, principal_id: str | None = None
    ) -> dict[str, Any]:
        """Derived compaction: the latest digest per session plus the most
        recent facts/decisions. The log remains authoritative."""
        now = time.time()
        with self._lock:
            params: list[Any] = [tenant_id, now]
            where = ["tenant_id=?", "(expires_at IS NULL OR expires_at>?)"]
            if principal_id is not None:
                where.append("principal_id=?")
                params.append(principal_id)
            rows = self._connection.execute(
                f"""
                SELECT * FROM hub_shared_events
                WHERE {" AND ".join(where)} AND scope='consensus'
                ORDER BY seq DESC LIMIT 200
                """,
                tuple(params),
            ).fetchall()
        events = [self._shared_event_dict(row) for row in rows]
        digests: dict[str, dict[str, Any]] = {}
        recent: list[dict[str, Any]] = []
        for event in events:
            if event["kind"] == "digest" and event["session_id"]:
                digests.setdefault(str(event["session_id"]), event)
            elif event["kind"] in {"fact", "decision"} and len(recent) < 20:
                recent.append(event)
        return {"digests": digests, "recent": recent}

    def purge_expired_shared_events(self) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM hub_shared_events WHERE expires_at IS NOT NULL AND expires_at<=?",
                (time.time(),),
            )
            return int(cursor.rowcount or 0)

    # ── Directory rows (scope='directory') ────────────────────────────────

    DIRECTORY_TTL_HOURS = 720  # 30 days idle before the row expires

    def upsert_directory_row(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: str,
        actor_id: str,
        node_id: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """Push one session directory row.

        The row is appended as a shared event with a fresh event_id (the
        latest event per session wins at read time), so incremental sync via
        seq works while the directory stays deduplicated.
        """
        node = self._node(node_id)
        if node["tenant_id"] != tenant_id:
            raise PermissionError("node does not belong to tenant")
        return self.append_shared_event(
            SharedEventAppend(
                scope="directory",
                kind="directory_upsert",
                payload=row,
                principal_id=principal_id,
                session_id=session_id,
                actor_id=actor_id,
                node_id=node_id,
                ttl_hours=self.DIRECTORY_TTL_HOURS,
                event_id=f"dir_{self._random_id()}",
            ),
            tenant_id=tenant_id,
        )

    def list_directory(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        after_seq: int = 0,
        query: str | None = None,
        status: str | None = None,
        actor_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List the latest directory row per session (deduplicated by seq)."""
        after_seq = max(after_seq, 0)
        limit = min(max(limit, 1), 500)
        now = time.time()
        where = [
            "scope='directory'",
            "tenant_id=?",
            "(expires_at IS NULL OR expires_at>?)",
        ]
        params: list[Any] = [tenant_id, now]
        if principal_id is not None:
            where.append("principal_id=?")
            params.append(principal_id)
        if query:
            where.append("payload_json LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_pattern(query)}%")
        if status:
            where.append("payload_json LIKE ? ESCAPE '\\'")
            params.append(f'%\"status\":\"{_like_pattern(status)}\"%')
        if actor_id is not None:
            where.append("actor_id=?")
            params.append(actor_id)
        params.append(after_seq)
        params.append(limit)
        rows = self._connection.execute(
            f"""
            SELECT d.* FROM hub_shared_events d
            JOIN (
                SELECT session_id, MAX(seq) AS max_seq FROM hub_shared_events
                WHERE {" AND ".join(where)} AND session_id IS NOT NULL
                GROUP BY session_id
            ) latest ON latest.session_id=d.session_id AND latest.max_seq=d.seq
            WHERE d.scope='directory' AND d.seq>?
            ORDER BY d.seq DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [self._shared_event_dict(row) for row in rows]

    def get_directory_row(
        self,
        *,
        tenant_id: str,
        principal_id: str | None,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            params: list[Any] = [tenant_id, session_id, time.time()]
            where = [
                "tenant_id=?",
                "session_id=?",
                "scope='directory'",
                "(expires_at IS NULL OR expires_at>?)",
            ]
            if principal_id is not None:
                where.append("principal_id=?")
                params.append(principal_id)
            row = self._connection.execute(
                f"""
                SELECT * FROM hub_shared_events
                WHERE {" AND ".join(where)}
                ORDER BY seq DESC LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            return self._shared_event_dict(row) if row is not None else None

    def artifacts_for_session(
        self,
        *,
        tenant_id: str,
        session_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Artifacts whose runs recorded this session (dsh_session_id)."""
        limit = min(max(limit, 1), 100)
        pattern = f'%"dsh_session_id":"{session_id}"%'
        with self._lock:
            runs = self._connection.execute(
                """
                SELECT run_id, task_id FROM hub_runs
                WHERE tenant_id=? AND result_json LIKE ? ESCAPE '\\'
                ORDER BY started_at DESC LIMIT 50
                """,
                (tenant_id, pattern),
            ).fetchall()
            run_ids = [str(run["run_id"]) for run in runs]
            task_ids = [str(run["task_id"]) for run in runs if run["task_id"]]
            if not run_ids and not task_ids:
                return []
            clauses: list[str] = []
            params: list[Any] = [tenant_id]
            if run_ids:
                clauses.append(f"run_id IN ({','.join('?' for _ in run_ids)})")
                params.extend(run_ids)
            if task_ids:
                clauses.append(f"task_id IN ({','.join('?' for _ in task_ids)})")
                params.extend(task_ids)
            params.append(limit)
            rows = self._connection.execute(
                f"""
                SELECT * FROM hub_artifacts
                WHERE tenant_id=? AND ({" OR ".join(clauses)})
                ORDER BY created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [
                {
                    "artifact_id": str(row["artifact_id"]),
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "name": str(row["name"]),
                    "media_type": str(row["media_type"]),
                    "uri": str(row["uri"]),
                }
                for row in rows
            ]

    def _shared_event_by_seq(self, seq: int) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM hub_shared_events WHERE seq=?", (seq,)
        ).fetchone()
        if row is None:
            raise LookupError("shared event not found")
        return self._shared_event_dict(row)

    @staticmethod
    def _shared_event_dict(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "event_id": str(row["event_id"]),
            "tenant_id": str(row["tenant_id"]),
            "principal_id": str(row["principal_id"]),
            "scope": str(row["scope"]),
            "kind": str(row["kind"]),
            "session_id": row["session_id"],
            "actor_id": row["actor_id"],
            "node_id": row["node_id"],
            "payload": _decode(str(row["payload_json"])),
            "ttl_hours": row["ttl_hours"],
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _random_id() -> str:
        import uuid

        return uuid.uuid4().hex


def _like_pattern(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
