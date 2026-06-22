"""Tiny reusable client for the switchback — what any Python app imports.

Two transports, tried in order:
  1. HTTP service at SCRAPER_ENGINE_URL (default http://localhost:8799) — one warm
     process, shared browser pool. Used when the service is up.
  2. `python -m switchback <url>` subprocess fallback, run from the switchback repo
     (SCRAPER_ENGINE_DIR, default: the repo this file lives in) — so it works
     even when the service isn't running and the package isn't installed.

Both return the same shape the engine emits everywhere:
    [{"url": ..., "source_method": ..., "markdown": ...}]   # successes only

    from python_client import scrape, search
    rows = scrape(["https://arxiv.org/abs/1706.03762"])

Stdlib-only (urllib + subprocess) so it has no install footprint of its own.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ENGINE_URL = os.getenv("SCRAPER_ENGINE_URL", "http://localhost:8799").rstrip("/")
# Repo root = parent of this clients/ dir, unless overridden.
ENGINE_DIR = os.getenv(
    "SCRAPER_ENGINE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
_HTTP_TIMEOUT = float(os.getenv("SCRAPER_CLIENT_HTTP_TIMEOUT_S", "300"))


def _http_post(path: str, payload: dict) -> list[dict]:
    req = urllib.request.Request(
        f"{ENGINE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _http_get(path: str, params: dict) -> list[dict]:
    url = f"{ENGINE_URL}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _service_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENGINE_URL}/healthz", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _cli_scrape(urls: list[str]) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "switchback", *urls],
        cwd=ENGINE_DIR, capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):  # 1 == "no successes", still valid JSON ([])
        raise RuntimeError(f"engine CLI failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout or "[]")


def scrape(urls: str | list[str]) -> list[dict]:
    """Scrape one or many URLs through the engine cascade. Successes only."""
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return []
    if _service_up():
        return _http_post("/scrape", {"urls": urls})
    return _cli_scrape(urls)


def search(query: str) -> list[dict]:
    """Query → ranked URLs (SearXNG), via the service. [] if the service is down."""
    if _service_up():
        return _http_get("/search", {"q": query})
    return []
