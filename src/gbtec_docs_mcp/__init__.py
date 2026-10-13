"""GBTEC Platform Documentation MCP server.

Structured search and page retrieval for AI agents, based on the
Sphinx/ReadTheDocs documentation at https://docs.gbtec.com/help/portal/

The server keeps a local SQLite (FTS5) index of the documentation.
If the index is older than a configurable age (default 24 hours), a
background thread rebuilds it and swaps it in atomically. All requests
are always served from the current (possibly just slightly stale) index.
"""

__version__ = "0.1.0"

from .ingest import (
    BASE_URL,
    LANGS,
    IndexStore,
    build_index,
    index_age_seconds,
    maybe_refresh_index,
    refresh_now,
)
from .server import create_server, main

__all__ = [
    "BASE_URL",
    "LANGS",
    "IndexStore",
    "build_index",
    "create_server",
    "index_age_seconds",
    "main",
    "maybe_refresh_index",
    "refresh_now",
]
