"""Application-owned backend primitives.

The files in this package are intentionally separate from the upstream
Douyin integration.  They own persistence and task lifecycle semantics so
the panel can evolve without replacing the browser adapter.
"""

from .run_store import RunStore
from .task_policy import account_timeout_seconds

__all__ = ["RunStore", "account_timeout_seconds"]
