from __future__ import annotations

import time
from typing import Any

from ..domain import (
    TenantRegistration,
)
from .base import (
    _json,
)

class TenantStore:
    """Tenant registry."""

    def create_tenant(self, item: TenantRegistration) -> dict[str, Any]:
        now = time.time()
        with self._condition, self._connection:
            self._connection.execute(
                """
                INSERT INTO hub_tenants(
                    tenant_id, display_name, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.tenant_id,
                    item.display_name,
                    _json(item.metadata),
                    now,
                    now,
                ),
            )
            return self._tenant(item.tenant_id)

    def list_tenants(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT tenant_id FROM hub_tenants ORDER BY tenant_id"
            ).fetchall()
            return [self._tenant(str(row["tenant_id"])) for row in rows]

    def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            return self._tenant(tenant_id)

