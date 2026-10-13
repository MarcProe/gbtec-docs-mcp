"""Ingestion of the GBTEC documentation into a local SQLite FTS5 index.

Sources
-------
* ``searchindex.js`` per language  – the Sphinx search index. We only use
  ``docnames`` and ``titles`` from it: it is the *authoritative* page list
  (every docname maps 1:1 to a rendered HTML page).
* The rendered HTML pages themselves – converted to Markdown and indexed
  with SQLite FTS5 (so we get proper full-text search with snippets,
  without depending on the Sphinx token index).

Atomicity
---------
A rebuild writes to a fresh ``index-<pid>.db`` file next to the live
database and is only promoted with :func:`os.replace` once it is fully
written. Readers never see a partial index.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://docs.gbtec.com/help/portal"
LANGS = ("de", "en")

DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60  # 24 hours (1 day)

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE pages (
    id        INTEGER PRIMARY KEY,
    lang      TEXT NOT NULL,
    path      TEXT NOT NULL,            -- e.g. "Design/chatbot"
    title     TEXT NOT NULL,
    url       TEXT NOT NULL,
    markdown  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_pages_lang_path ON pages(lang, path);
CREATE VIRTUAL TABLE pages_fts USING fts5(
    title, markdown,
    content='pages', content_rowid='id'
);
CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, markdown)
    VALUES (new.id, new.title, new.markdown);
END;
CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown)
    VALUES ('delete', old.id, old.title, old.markdown);
END;
CREATE TRIGGER pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, markdown)
    VALUES ('delete', old.id, old.title, old.markdown);
    INSERT INTO pages_fts(rowid, title, markdown)
    VALUES (new.id, new.title, new.markdown);
END;
"""

# ---------------------------------------------------------------------------
# searchindex.js parsing
# ---------------------------------------------------------------------------

_DOCNAMES_RE = re.compile(r"docnames:\[(.*?)\]", re.S)
_TITLES_RE = re.compile(r"titles:\[(.*?)\],titleterms", re.S)
_JS_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _js_strings(blob: str) -> list[str]:
    """Extract the (double quoted) string literals of a JS array body."""
    out: list[str] = []
    for m in _JS_STRING_RE.finditer(blob):
        s = m.group(1)
        s = (
            s.replace('\\"', '"')
            .replace("\\\\", "\\")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
        # Sphinx may escape non-ascii as \uXXXX
        s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda u: chr(int(u.group(1), 16)), s)
        out.append(s)
    return out


def parse_searchindex(raw: str) -> list[tuple[str, str]]:
    """Return (docname, title) pairs from a Sphinx ``searchindex.js`` body."""
    m = _DOCNAMES_RE.search(raw)
    if not m:
        raise ValueError("searchindex.js: 'docnames' not found")
    docnames = _js_strings(m.group(1))

    titles: list[str] = []
    tm = _TITLES_RE.search(raw)
    if tm:
        titles = _js_strings(tm.group(1))
    # Fall back to the docname tail if titles are missing.
    while len(titles) < len(docnames):
        titles.append(docnames[len(titles)].rsplit("/", 1)[-1])
    return list(zip(docnames, titles))


# ---------------------------------------------------------------------------
# HTML -> Markdown
# ---------------------------------------------------------------------------

def _html_to_markdown(html: str, base_url: str) -> str:
    from bs4 import BeautifulSoup
    from html2text import HTML2Text

    soup = BeautifulSoup(html, "html.parser")

    # Keep only the document body (drop navbars, sidebars, RTD footer).
    body = soup.find("div", class_="body") or soup.find("article") or soup.body

    # Make relative links absolute so the AI can follow them.
    base = httpx.URL(base_url)
    for a in body.find_all("a", href=True):
        a["href"] = str(base.join(a["href"]))

    # Drop UI noise: breadcrumbs, header anchor links, RTD chrome.
    # (order matters: breadcrumbs first, then the rest)
    for sel in (
        "div.breadcrumbs",
        "div.admonition-title a.headerlink",
        "a.headerlink",
        "div.rst-versions",
        "div.related",
        "nav",
    ):
        for el in body.select(sel):
            el.decompose()

    text = HTML2Text()
    text.body_width = 0
    text.ignore_images = False
    text.ignore_emphasis = False
    md = text.handle(str(body))
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# ---------------------------------------------------------------------------
# Index store
# ---------------------------------------------------------------------------

def cache_dir() -> Path:
    base = os.environ.get("GBTEC_DOCS_MCP_CACHE")
    if base:
        p = Path(base).expanduser()
    else:
        p = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "gbtec-docs-mcp"
    p.mkdir(parents=True, exist_ok=True)
    return p


INDEX_PATH: Path = cache_dir() / "index.db"


@dataclass
class IndexStats:
    built_at: str
    age_seconds: float
    pages: int
    pages_by_lang: dict[str, int]
    size_bytes: int


class IndexStore:
    """Thin reader facade over the live SQLite index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or INDEX_PATH

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def is_available(self) -> bool:
        return self.path.exists()

    def stats(self) -> IndexStats | None:
        if not self.is_available():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='built_at'").fetchone()
            if row is None:
                return None
            built_at = row["value"]
            counts: dict[str, int] = {}
            total = 0
            for lang, n in conn.execute(
                "SELECT lang, COUNT(*) FROM pages GROUP BY lang"
            ):
                counts[lang] = n
                total += n
            try:
                built_ts = time.mktime(time.strptime(built_at, "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                built_ts = time.time()
        return IndexStats(
            built_at=built_at,
            age_seconds=max(0.0, time.time() - built_ts),
            pages=total,
            pages_by_lang=counts,
            size_bytes=self.path.stat().st_size,
        )

    def search(
        self, query: str, lang: str = "all", limit: int = 10
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if lang != "all":
                return conn.execute(
                    """
                    SELECT p.lang, p.path, p.title, p.url,
                           snippet(pages_fts, 1, '>>', '<<', ' … ', 40) AS snippet,
                           bm25(pages_fts) AS rank
                    FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid
                    WHERE pages_fts MATCH ? AND p.lang = ?
                    ORDER BY rank LIMIT ?
                    """,
                    (query, lang, limit),
                ).fetchall()
            return conn.execute(
                """
                SELECT p.lang, p.path, p.title, p.url,
                       snippet(pages_fts, 1, '>>', '<<', ' … ', 40) AS snippet,
                       bm25(pages_fts) AS rank
                FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid
                WHERE pages_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (query, limit),
            ).fetchall()

    def get_page(self, lang: str, path: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM pages WHERE lang = ? AND path = ?", (lang, path)
            ).fetchone()

    def list_sections(self, lang: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT path, title, url FROM pages "
                "WHERE lang = ? ORDER BY path",
                (lang,),
            ).fetchall()


def index_age_seconds(path: Path | None = None) -> float | None:
    stats = IndexStore(path).stats()
    return stats.age_seconds if stats else None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def build_index(
    target: Path | None = None,
    langs: tuple[str, ...] = LANGS,
    session: httpx.Client | None = None,
    force: bool = False,
) -> IndexStats:
    """Build a fresh index and promote it atomically.

    ``force`` skips the per-page cache (by updated_at) and refetches
    everything.
    """
    from bs4 import BeautifulSoup  # noqa: F401  (ensures dep is present)

    target = target or INDEX_PATH
    tmp = target.with_name(f".index-building-{os.getpid()}.db")
    if tmp.exists():
        tmp.unlink()

    own_session = session is None
    client = session or httpx.Client(
        headers={"User-Agent": "gbtec-docs-mcp/0.1"},
        timeout=30.0,
        follow_redirects=True,
    )

    # Reuse unchanged pages from the live index to keep rebuilds cheap.
    existing: dict[tuple[str, str], tuple[str, str]] = {}
    if target.exists():
        try:
            with IndexStore(target).connect() as conn:
                for row in conn.execute(
                    "SELECT lang, path, title, markdown FROM pages"
                ):
                    existing[(row["lang"], row["path"])] = (
                        row["title"],
                        row["markdown"],
                    )
        except sqlite3.DatabaseError:
            existing = {}

    conn = sqlite3.connect(tmp)
    # Keep the building db in rollback journal mode so it ends up as a
    # single self-contained file before we promote it atomically.
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (_now_iso(),))

    pages_count = 0
    try:
        for lang in langs:
            base = f"{BASE_URL}/{lang}/"
            raw = client.get(base + "searchindex.js").text
            docnames = parse_searchindex(raw)
            for path, title in docnames:
                url = base + path + ".html"
                cached = existing.get((lang, path))
                if cached is not None and not force:
                    md = cached[1]
                else:
                    r = client.get(url)
                    r.raise_for_status()
                    md = _html_to_markdown(r.text, base)
                conn.execute(
                    "INSERT INTO pages (lang, path, title, url, markdown, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lang, path, title, url, md, _now_iso()),
                )
                pages_count += 1
        conn.commit()
    finally:
        if own_session:
            client.close()
        conn.close()

    # Atomic promotion: tmp lives next to target, so os.replace is atomic.
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, target)

    stats = IndexStore(target).stats()
    if stats is None:
        raise RuntimeError("index build reported but stats unreadable")
    log.info("index built: %d pages in %.1fs", pages_count, stats.age_seconds)
    return stats


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------

_refresh_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_last_check = 0.0


def _do_refresh() -> None:
    global _refresh_thread
    try:
        build_index()
    except Exception:  # pragma: no cover - defensive
        log.exception("background index refresh failed")
    finally:
        with _refresh_lock:
            _refresh_thread = None


def refresh_now() -> None:
    """Kick off a background rebuild (no-op if one is already running)."""
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(
            target=_do_refresh, name="gbtec-docs-refresh", daemon=True
        )
        _refresh_thread.start()


def maybe_refresh_index(
    max_age_seconds: float | None = None,
) -> str:
    """Check the index age; start a background refresh when stale.

    Returns a short status string suitable for tool output:
    ``ok``, ``stale-refreshing``, ``missing-building`` or ``refresh-running``.
    """
    global _last_check
    max_age = max_age_seconds if max_age_seconds is not None else float(
        os.environ.get("GBTEC_DOCS_MCP_MAX_AGE", DEFAULT_MAX_AGE_SECONDS)
    )
    # Throttle the *check* itself to at most once per minute.
    now = time.time()
    if now - _last_check < 60:
        return "ok"
    _last_check = now

    age = index_age_seconds()
    if age is None:
        refresh_now()
        return "missing-building"
    if age > max_age:
        refresh_now()
        return "stale-refreshing"
    return "ok"


def _graceful_handler(signum, frame):  # pragma: no cover
    os._exit(0)


def install_signal_guard() -> None:
    """Make sure a background build cannot wedge shutdown of the stdio
    transport (which dies on broken pipe / EOF)."""
    try:
        signal.signal(signal.SIGTERM, _graceful_handler)
    except (ValueError, OSError):
        pass
