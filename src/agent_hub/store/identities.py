from __future__ import annotations

import time
from typing import Any

from ..domain import (
    ActorRegistration,
    NodeRegistration,
    PrincipalRegistration,
)
from .base import (
    _json,
)

class IdentityStore:
    """Principal, actor and node registrations."""

    def register_principal(
        self, item: PrincipalRegistration, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._connection.execute(
                """
                INSERT INTO hub_principals(
                    principal_id, kind, display_name, metadata_json, tenant_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    tenant_id,
                    now,
                    now,
                ),
            )
            return self._principal(item.principal_id)

    def register_actor(
        self, item: ActorRegistration, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._require("hub_principals", "principal_id", item.principal_id)
            self._connection.execute(
                """
                INSERT INTO hub_actors(
                    actor_id, principal_id, kind, display_name,
                    capabilities_json, metadata_json, tenant_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    principal_id=excluded.principal_id,
                    kind=excluded.kind,
                    display_name=excluded.display_name,
                    capabilities_json=excluded.capabilities_json,
                    metadata_json=excluded.metadata_json,
                    tenant_id=excluded.tenant_id,
                    updated_at=excluded.updated_at
                """,
                (
                    item.actor_id,
                    item.principal_id,
                    item.kind,
                    item.display_name,
                    _json(item.capabilities),
                    _json(item.metadata),
                    tenant_id,
                    now,
                    now,
                ),
            )
            return self._actor(item.actor_id)

    def register_node(
        self, item: NodeRegistration, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._require("hub_actors", "actor_id", item.actor_id)
            self._connection.execute(
                """
                INSERT INTO hub_nodes(
                    node_id, actor_id, display_name, capabilities_json,
                    metadata_json, tenant_id, status, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'online', ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    actor_id=excluded.actor_id,
                    display_name=excluded.display_name,
                    capabilities_json=excluded.capabilities_json,
                    metadata_json=excluded.metadata_json,
                    tenant_id=excluded.tenant_id,
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
                    tenant_id,
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

    def list_principals(
        self, *, tenant_id: str | None = None, principal_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if principal_id is not None:
                rows = self._connection.execute(
                    "SELECT principal_id FROM hub_principals WHERE principal_id=?",
                    (principal_id,),
                ).fetchall()
            elif tenant_id is None:
                rows = self._connection.execute(
                    "SELECT principal_id FROM hub_principals ORDER BY principal_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT principal_id FROM hub_principals
                    WHERE tenant_id=? ORDER BY principal_id
                    """,
                    (tenant_id,),
                ).fetchall()
            return [self._principal(str(row["principal_id"])) for row in rows]

    def list_actors(
        self, *, tenant_id: str | None = None, principal_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if principal_id is not None:
                rows = self._connection.execute(
                    """
                    SELECT actor_id FROM hub_actors
                    WHERE principal_id=? ORDER BY actor_id
                    """,
                    (principal_id,),
                ).fetchall()
            elif tenant_id is None:
                rows = self._connection.execute(
                    "SELECT actor_id FROM hub_actors ORDER BY actor_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT actor_id FROM hub_actors
                    WHERE tenant_id=? ORDER BY actor_id
                    """,
                    (tenant_id,),
                ).fetchall()
            return [self._actor(str(row["actor_id"])) for row in rows]

    def list_nodes(
        self, *, tenant_id: str | None = None, principal_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection:
            self._mark_stale_nodes_offline()
            if principal_id is not None:
                rows = self._connection.execute(
                    """
                    SELECT hub_nodes.node_id FROM hub_nodes
                    JOIN hub_actors ON hub_actors.actor_id = hub_nodes.actor_id
                    WHERE hub_actors.principal_id=? ORDER BY hub_nodes.node_id
                    """,
                    (principal_id,),
                ).fetchall()
            elif tenant_id is None:
                rows = self._connection.execute(
                    "SELECT node_id FROM hub_nodes ORDER BY node_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT node_id FROM hub_nodes
                    WHERE tenant_id=? ORDER BY node_id
                    """,
                    (tenant_id,),
                ).fetchall()
            return [self._node(str(row["node_id"])) for row in rows]
