from __future__ import annotations

from .artifacts import ArtifactStore
from .base import StoreBase
from .identities import IdentityStore
from .runs import RunStore
from .shared import SharedStore
from .stats import StatsStore
from .tasks import TaskStore
from .questions import QuestionStore
from .tenants import TenantStore
from .tokens import TokenStore
from .users import UserStore


class AgentHubStore(
    StoreBase,
    IdentityStore,
    TaskStore,
    QuestionStore,
    RunStore,
    ArtifactStore,
    TenantStore,
    TokenStore,
    UserStore,
    SharedStore,
    StatsStore,
):
    """Durable local-first coordination state shared by Agent Hosts.

    The store deliberately contains no model-specific behavior. Pi is the first
    runtime adapter, while actors, tasks, runs, and artifacts remain portable.
    """


__all__ = ["AgentHubStore"]
