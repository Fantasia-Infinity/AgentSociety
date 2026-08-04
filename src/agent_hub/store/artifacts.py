from __future__ import annotations

import time
import uuid
from typing import Any

from ..domain import (
    ArtifactSubmission,
)
from .base import (
    _json,
)

class ArtifactStore:
    """Artifact metadata."""

    def add_artifact(
        self, item: ArtifactSubmission, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self._condition, self._connection:
            creator = self._actor(item.created_by_actor_id)
            if creator["tenant_id"] != tenant_id:
                raise PermissionError("creator actor does not belong to tenant")
            task_tenant_id: str | None = None
            if item.task_id is not None:
                task = self._task(item.task_id)
                task_tenant_id = task["tenant_id"]
            if item.run_id is not None:
                run = self._run(item.run_id)
                if item.task_id is not None and run["task_id"] != item.task_id:
                    raise ValueError("artifact task_id and run_id do not match")
                if run["tenant_id"] != tenant_id:
                    raise PermissionError("run does not belong to tenant")
            if item.task_id is not None and task_tenant_id != tenant_id:
                raise PermissionError("task does not belong to tenant")
            self._connection.execute(
                """
                INSERT INTO hub_artifacts(
                    artifact_id, task_id, run_id, name, media_type, uri, sha256,
                    size_bytes, created_by_actor_id, metadata_json, tenant_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    tenant_id,
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
                    tenant_id=tenant_id,
                    now=now,
                )
            return self._artifact(artifact_id)

    def list_artifacts(
        self, *, limit: int = 100, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 500)
        with self._lock:
            if tenant_id is None:
                rows = self._connection.execute(
                    """
                    SELECT artifact_id FROM hub_artifacts
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT artifact_id FROM hub_artifacts
                    WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
            return [self._artifact(str(row["artifact_id"])) for row in rows]

