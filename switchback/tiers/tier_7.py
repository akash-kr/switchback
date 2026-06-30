"""Tier 4 — Firecrawl (paid, last resort).

Env-gated: set SCRAPER_DISABLE_FIRECRAWL to skip this tier entirely (URL is then
dropped). Every invocation is audited and feeds the botwall promotion counter, so
hosts that keep needing it get auto-skipped. Needs FIRECRAWL_API_KEY.
"""
from __future__ import annotations

import os
import threading

from ..normalize import active_format, render
from ..policy.gates import check

NAME = "tier_7"
PAID = True

# Per-tier wall-clock cap (seconds); override with SCRAPER_TIER_7_TIMEOUT_S.
# This paid last resort was previously unbounded; 15s bounds it like the rest, but
# Firecrawl scrapes can legitimately run longer — raise this if hard hosts get cut
# off at the finish line (you may still be billed for a scrape killed here).
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_7_TIMEOUT_S", "15"))


def disabled() -> bool:
    return bool(os.getenv("SCRAPER_DISABLE_FIRECRAWL"))


def _scrape(url: str, fmt: str) -> str:
    from firecrawl import Firecrawl
    app = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
    if fmt == "markdown":
        doc = app.scrape(url, formats=["markdown"])
        d = doc.model_dump() if hasattr(doc, "model_dump") else (doc if isinstance(doc, dict) else {})
        return check(url, (d.get("markdown") or "").strip())
    # Non-default formats: fetch HTML and derive every shape through normalize, so
    # html / html_selectors / markdown_trimmed match the rest of the cascade.
    doc = app.scrape(url, formats=["html"])
    d = doc.model_dump() if hasattr(doc, "model_dump") else (doc if isinstance(doc, dict) else {})
    return check(url, render(d.get("html") or "", base_url=url, fmt=fmt))


def fetch(url: str) -> str:
    # Run in a dedicated thread: the Firecrawl SDK sets an asyncio event loop on
    # the calling thread, which then makes a later sync-Playwright browser tier in
    # the same batch raise "Sync API inside the asyncio loop". A worker thread
    # confines that loop so the browser tiers stay usable across a multi-URL run.
    # active_format() is thread-local, so read it here (main thread) and pass it in.
    box: dict = {}
    fmt = active_format()

    def work():
        try:
            box["md"] = _scrape(url, fmt)
        except BaseException as e:  # noqa: BLE001 — re-raised to the caller below
            box["err"] = e

    t = threading.Thread(target=work, name="tier_7-firecrawl", daemon=True)
    t.start()
    t.join(_TIMEOUT_S)
    if t.is_alive():
        raise TimeoutError(
            f"firecrawl exceeded {_TIMEOUT_S}s "
            "(raise SCRAPER_TIER_7_TIMEOUT_S for slow hosts)")
    if "err" in box:
        raise box["err"]
    return box["md"]
