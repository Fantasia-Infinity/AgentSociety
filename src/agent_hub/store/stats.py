from __future__ import annotations


class StatsStore:
    """Dashboard statistics."""

    def stats(self, *, tenant_id: str | None = None) -> dict[str, int]:
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
                if tenant_id is None:
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()
                else:
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id=?",
                        (tenant_id,),
                    ).fetchone()
                result[name] = int(row["count"]) if row is not None else 0
            return result

