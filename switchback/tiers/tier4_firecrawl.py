"""Tier 4 — Firecrawl (paid, last resort).

Env-gated: set SCRAPER_DISABLE_FIRECRAWL to skip this tier entirely (URL is then
dropped). Every invocation is audited and feeds the botwall promotion counter, so
hosts that keep needing it get auto-skipped. Needs FIRECRAWL_API_KEY.
"""
from __future__ import annotations

import os
import threading

from ..policy.gates import check

NAME = "tier4_firecrawl"
PAID = True


def disabled() -> bool:
    return bool(os.getenv("SCRAPER_DISABLE_FIRECRAWL"))


def _scrape(url: str) -> str:
    from firecrawl import Firecrawl
    app = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
    doc = app.scrape(url, formats=["markdown"])
    d = doc.model_dump() if hasattr(doc, "model_dump") else (doc if isinstance(doc, dict) else {})
    return check(url, (d.get("markdown") or "").strip())


def fetch(url: str) -> str:
    # Run in a dedicated thread: the Firecrawl SDK sets an asyncio event loop on
    # the calling thread, which then makes a later sync-Playwright browser tier in
    # the same batch raise "Sync API inside the asyncio loop". A worker thread
    # confines that loop so the browser tiers stay usable across a multi-URL run.
    box: dict = {}

    def work():
        try:
            box["md"] = _scrape(url)
        except BaseException as e:  # noqa: BLE001 — re-raised to the caller below
            box["err"] = e

    t = threading.Thread(target=work, name="tier4-firecrawl", daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box["md"]
