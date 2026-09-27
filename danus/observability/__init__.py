"""danus.observability — strictly read-only live activity and result views.

    python -m danus.observability --project <dir> [--port 8099]

Runtime adapters only read existing records. They never start, stop, steer,
verify, or otherwise change a research process. See README.md for the sources.
"""
from __future__ import annotations

from .app import (
    CHANNELS,
    app,
    build_channel,
    build_channels,
    build_factgraph,
    build_overview,
    main,
)
from .activity import router as activity_router

app.include_router(activity_router)

__all__ = [
    "app", "main", "CHANNELS", "build_overview", "build_factgraph",
    "build_channels", "build_channel",
]
