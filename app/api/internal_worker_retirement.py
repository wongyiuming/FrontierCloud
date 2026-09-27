"""Remove retired Compute Worker control routes from the production router.

Legacy worker tables and helper functions may remain temporarily for backup/restore
compatibility, but no production HTTP surface may lease or complete worker jobs.
"""
from __future__ import annotations

from app.api import internal_nodes


RETIRED_WORKER_PREFIX = "/internal/v1/jobs/"


def install() -> None:
    """Idempotently remove every retired worker endpoint before app registration."""
    internal_nodes.router.routes[:] = [
        route
        for route in internal_nodes.router.routes
        if not str(getattr(route, "path", "")).startswith(RETIRED_WORKER_PREFIX)
    ]
