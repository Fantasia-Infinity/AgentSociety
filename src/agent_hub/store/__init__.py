from __future__ import annotations

from .artifacts import ArtifactStore
from .base import StoreBase
from .identities import IdentityStore
from .runs import RunStore
from .stats import StatsStore
from .tasks import TaskStore
from .tenants import TenantStore
from .tokens import TokenStore


class AgentHubStore(
    StoreBase,
    IdentityStore,
    TaskStore,
    RunStore,
    ArtifactStore,
    TenantStore,
    TokenStore,
    StatsStore,
):
    """Durable local-first coordination state shared by Agent Hosts.

    The store deliberately contains no model-specific behavior. Pi is the first
    runtime adapter, while actors, tasks, runs, and artifacts remain portable.
    """


__all__ = ["AgentHubStore"]
