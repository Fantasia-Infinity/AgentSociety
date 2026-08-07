from __future__ import annotations


class StatsStore:
    """Dashboard statistics."""

    def stats(
        self, *, tenant_id: str | None = None, principal_id: str | None = None
    ) -> dict[str, int]:
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
                if principal_id is not None:
                    if name == "nodes":
                        row = self._connection.execute(
                            """
                            SELECT COUNT(*) AS count FROM hub_nodes
                            JOIN hub_actors ON hub_actors.actor_id = hub_nodes.actor_id
                            WHERE hub_actors.principal_id=?
                            """,
                            (principal_id,),
                        ).fetchone()
                    elif name == "artifacts":
                        row = self._connection.execute(
                            """
                            SELECT COUNT(*) AS count FROM hub_artifacts
                            JOIN hub_actors ON hub_actors.actor_id = hub_artifacts.created_by_actor_id
                            WHERE hub_actors.principal_id=?
                            """,
                            (principal_id,),
                        ).fetchone()
                    elif name == "principals":
                        row = self._connection.execute(
                            "SELECT COUNT(*) AS count FROM hub_principals WHERE principal_id=?",
                            (principal_id,),
                        ).fetchone()
                    else:
                        row = self._connection.execute(
                            f"SELECT COUNT(*) AS count FROM {table} WHERE principal_id=?",
                            (principal_id,),
                        ).fetchone()
                elif tenant_id is None:
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()
                else:
                    row = self._connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id=?",
                        (tenant_id,),
                    ).fetchone()
                result[name] = int(row["count"]) if row is not None else 0
            if principal_id is not None:
                node_rows = self._connection.execute(
                    """
                    SELECT status, COUNT(*) AS n FROM hub_nodes
                    JOIN hub_actors ON hub_actors.actor_id = hub_nodes.actor_id
                    WHERE hub_actors.principal_id=? GROUP BY status
                    """,
                    (principal_id,),
                ).fetchall()
                task_rows = self._connection.execute(
                    """
                    SELECT status, COUNT(*) AS n FROM hub_tasks
                    WHERE principal_id=? GROUP BY status
                    """,
                    (principal_id,),
                ).fetchall()
            elif tenant_id is None:
                node_rows = self._connection.execute(
                    "SELECT status, COUNT(*) AS n FROM hub_nodes GROUP BY status"
                ).fetchall()
                task_rows = self._connection.execute(
                    "SELECT status, COUNT(*) AS n FROM hub_tasks GROUP BY status"
                ).fetchall()
            else:
                node_rows = self._connection.execute(
                    """
                    SELECT status, COUNT(*) AS n FROM hub_nodes
                    WHERE tenant_id=? GROUP BY status
                    """,
                    (tenant_id,),
                ).fetchall()
                task_rows = self._connection.execute(
                    """
                    SELECT status, COUNT(*) AS n FROM hub_tasks
                    WHERE tenant_id=? GROUP BY status
                    """,
                    (tenant_id,),
                ).fetchall()
            result["nodes_online"] = sum(
                int(row["n"]) for row in node_rows if row["status"] == "online"
            )
            for status in (
                "submitted",
                "working",
                "completed",
                "failed",
                "cancelled",
            ):
                result[f"tasks_{status}"] = next(
                    (
                        int(row["n"])
                        for row in task_rows
                        if row["status"] == status
                    ),
                    0,
                )
            return result
