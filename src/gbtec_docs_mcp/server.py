"""FastMCP (stdio) server exposing the GBTEC documentation index."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .ingest import (
    BASE_URL,
    LANGS,
    IndexStore,
    index_age_seconds,
    install_signal_guard,
    maybe_refresh_index,
    refresh_now,
)

log = logging.getLogger(__name__)

SERVER_NAME = "gbtec-docs"
SERVER_DESCRIPTION = (
    "Search and read the GBTEC platform documentation "
    "(BIC Process Design, EAM, GRC Management, Work Orchestrator) "
    "from https://docs.gbtec.com – languages: de, en."
)


def create_server() -> FastMCP:
    store = IndexStore()
    mcp = FastMCP(SERVER_NAME, instructions=SERVER_DESCRIPTION)

    @mcp.tool()
    def search_docs(
        query: str,
        lang: Literal["de", "en", "all"] = "all",
        limit: int = 10,
    ) -> str:
        """Full-text search over the GBTEC platform documentation.

        Args:
            query: Search phrase. Words are combined (AND). For a literal
                phrase use double quotes, e.g. ``"Anmeldesperre"``.
                Prefix a word with ``-`` to exclude it.
            lang: ``de``, ``en`` or ``all`` (default ``all``).
            limit: Maximum number of results (1-25, default 10).

        Returns:
            JSON with matching pages: lang, path, title, url, snippet,
            rank. Follow up with ``get_page`` using the returned path.
        """
        maybe_refresh_index()
        limit = max(1, min(int(limit), 25))
        try:  # noqa: SIM105
            rows = store.search(query, lang=lang, limit=limit)
        except sqlite3.OperationalError:
            # FTS5 syntax error on odd queries – retry with each word
            # quoted individually (safe fallback).
            words = [f'"{w.replace(chr(34), "")}"' for w in query.split() if w]
            rows = store.search(" ".join(words), lang=lang, limit=limit)

        out = [
            {
                "lang": r["lang"],
                "path": r["path"],
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"],
            }
            for r in rows
        ]
        header = (
            f"Found {len(out)} result(s) for '{query}' (lang={lang}). "
            f"Use get_page(lang, path) for full content. "
            f"Source: {BASE_URL}"
        )
        return header + "\n\n" + json.dumps(out, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_page(lang: Literal["de", "en"], path: str) -> str:
        """Return the full Markdown content of one documentation page.

        Args:
            lang: ``de`` or ``en``.
            path: Page path from ``search_docs`` results, e.g.
                ``"Design/chatbot"`` or ``"releasenotes"``.
        """
        maybe_refresh_index()
        row = store.get_page(lang, path)
        if row is None:
            # Try case-insensitive fallback on path.
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM pages WHERE lang=? AND lower(path)=lower(?)",
                    (lang, path),
                ).fetchone()
        if row is None:
            return (
                f"Page '{path}' not found for lang '{lang}'. "
                "Use list_sections to see available pages, or search_docs."
            )
        return (
            f"Title: {row['title']}\nURL: {row['url']}\n"
            f"Product: {row['path'].split('/')[0]}\n"
            f"{'=' * 60}\n\n{row['markdown']}"
        )

    @mcp.tool()
    def list_sections(lang: Literal["de", "en"] = "de") -> str:
        """List all documentation pages (title + path) for one language.

        The path is grouped by product: Design (BIC Process Design),
        EAM, GRCM (GRC Management), WO (Work Orchestrator) plus
        cross-product pages (index, mygbtec, releasenotes, publicApi, ...).
        """
        maybe_refresh_index()
        rows = store.list_sections(lang)
        lines: list[str] = []
        cur = ""
        for r in rows:
            top = r["path"].split("/")[0]
            if top != cur:
                cur = top
                lines.append(f"\n[{top}]")
            lines.append(f"  {r['path']}  –  {r['title']}")
        return (
            f"{len(rows)} pages for lang='{lang}'. "
            f"Use get_page(lang, path) to read a page.\n" + "\n".join(lines)
        )

    @mcp.tool()
    def index_status() -> str:
        """Show the state of the local documentation index:
        age, page counts per language and size. A background refresh
        is started automatically when the index is older than 24 hours."""
        maybe_refresh_index()
        stats = store.stats()
        if stats is None:
            return "Index is empty; a background build was just started."
        return (
            f"Index built at: {stats.built_at} "
            f"({stats.age_seconds / 60:.0f} min ago)\n"
            f"Pages: {stats.pages} total "
            f"({', '.join(f'{k}: {v}' for k, v in sorted(stats.pages_by_lang.items()))})\n"
            f"Size: {stats.size_bytes / 1024:.0f} KB\n"
            f"Source: {BASE_URL}"
        )

    return mcp


def main() -> None:
    import os
    import sys

    logging.basicConfig(
        level=getattr(logging, os.environ.get("GBTEC_DOCS_MCP_LOG", "INFO").upper(), logging.INFO),
        stream=sys.stderr,
    )
    install_signal_guard()
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
