"""URL → result cache, so an already-scraped page isn't scraped again.

Off by default (article content goes stale): set ``SCRAPER_CONTENT_TTL_S`` to a
positive number of seconds to enable it, e.g. 86400 to dedupe re-scrapes within a
day. A hit short-circuits the whole cascade before any tier (or proxy byte) runs.

Backed by stdlib sqlite (``state/content_cache.db``), not a JSON blob: at
curiouscats' ~300k URLs/month a single JSON file would be reloaded and rewritten
in full on every access. sqlite keys by URL and stays O(1). The cache is keyed by
normalised URL (fragment dropped); the egress scope is irrelevant to the *content*
so it isn't part of the key.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
_STATE_DIR = os.getenv("SCRAPER_STATE_DIR", _DEFAULT_STATE_DIR)
DB_PATH = os.path.join(_STATE_DIR, "content_cache.db")

_TTL_S = float(os.getenv("SCRAPER_CONTENT_TTL_S", "0"))  # 0 = disabled

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def enabled() -> bool:
    return _TTL_S > 0


def _norm(url: str) -> str:
    """Drop the fragment; everything else is significant (query strings select
    content)."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    with _LOCK:
        if _CONN is None:
            os.makedirs(_STATE_DIR, exist_ok=True)
            c = sqlite3.connect(DB_PATH, check_same_thread=False)
            c.execute("CREATE TABLE IF NOT EXISTS cache ("
                      "url TEXT PRIMARY KEY, markdown TEXT, source_method TEXT, ts REAL)")
            c.commit()
            _CONN = c
    return _CONN


def get(url: str) -> tuple[str, str] | None:
    """Return ``(markdown, source_method)`` for a fresh cache hit, else None."""
    if not enabled():
        return None
    conn = _conn()  # NB: acquires _LOCK itself — must be outside the lock below
    try:
        with _LOCK:
            row = conn.execute(
                "SELECT markdown, source_method, ts FROM cache WHERE url=?",
                (_norm(url),)).fetchone()
    except Exception as e:
        logger.warning(f"content_cache: read failed: {e}")
        return None
    if not row:
        return None
    markdown, source_method, ts = row
    if time.time() - ts > _TTL_S:
        return None
    return markdown, source_method


def put(url: str, markdown: str, source_method: str) -> None:
    """Store a successful scrape. No-op when disabled."""
    if not enabled():
        return
    conn = _conn()  # NB: acquires _LOCK itself — must be outside the lock below
    try:
        with _LOCK:
            conn.execute("INSERT OR REPLACE INTO cache (url, markdown, source_method, ts) "
                         "VALUES (?, ?, ?, ?)",
                         (_norm(url), markdown, source_method, time.time()))
            conn.commit()
    except Exception as e:
        logger.warning(f"content_cache: write failed: {e}")
