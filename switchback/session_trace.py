"""Opt-in Playwright trace capture for the browser tiers (req 16).

Off by default. Set ``SCRAPER_TRACE_SESSION=1`` to record a Playwright trace
(screenshots + DOM snapshots + network) for every browser-tier attempt; each is
written as a self-contained zip under ``state/traces/`` and is openable with
``playwright show-trace <zip>``. The HTTP server exposes list / fetch / delete
endpoints so traces can be pulled and cleaned up on demand.

Capture is wrapped so a tracing failure never breaks a scrape — it just means no
trace for that attempt. Traces are heavyweight (MBs each); keep this off in
steady state and flip it on to debug a specific host.
"""
from __future__ import annotations

import logging
import os
import re
import time

from .policy.gates import host_of

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
_STATE_DIR = os.getenv("SCRAPER_STATE_DIR", _DEFAULT_STATE_DIR)
TRACE_DIR = os.path.join(_STATE_DIR, "traces")

# Trace ids are the zip filename stem; constrain to a safe charset so a request
# id can never escape TRACE_DIR.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def enabled() -> bool:
    return os.getenv("SCRAPER_TRACE_SESSION") in ("1", "true", "True")


def start(context, url: str) -> bool:
    """Begin tracing on a browser context. No-op (returns False) when disabled or
    if the context doesn't support tracing."""
    if not enabled():
        return False
    try:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        return True
    except Exception as e:
        logger.warning(f"session_trace: start failed: {e}")
        return False


def stop(context, url: str) -> str | None:
    """Stop tracing and write the zip; returns its path (or None on failure)."""
    if not enabled():
        return None
    os.makedirs(TRACE_DIR, exist_ok=True)
    name = f"{host_of(url) or 'unknown'}-{int(time.time() * 1000)}.zip"
    path = os.path.join(TRACE_DIR, name)
    try:
        context.tracing.stop(path=path)
        logger.info(f"session_trace: wrote {path}")
        return path
    except Exception as e:
        logger.warning(f"session_trace: stop failed: {e}")
        return None


# ── server-side management ───────────────────────────────────────────────────

def list_traces() -> list[dict]:
    """All captured traces, newest first: id, bytes, modified-at (epoch)."""
    if not os.path.isdir(TRACE_DIR):
        return []
    out = []
    for fn in os.listdir(TRACE_DIR):
        if not fn.endswith(".zip"):
            continue
        p = os.path.join(TRACE_DIR, fn)
        st = os.stat(p)
        out.append({"id": fn[:-4], "bytes": st.st_size, "modified": st.st_mtime})
    return sorted(out, key=lambda t: -t["modified"])


def path_for(trace_id: str) -> str | None:
    """Resolve a trace id to its zip path, or None if missing/invalid."""
    if not _ID_RE.match(trace_id or ""):
        return None
    p = os.path.join(TRACE_DIR, f"{trace_id}.zip")
    return p if os.path.exists(p) else None


def delete(trace_id: str) -> bool:
    p = path_for(trace_id)
    if not p:
        return False
    os.remove(p)
    return True
