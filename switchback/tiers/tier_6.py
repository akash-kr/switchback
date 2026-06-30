"""Tier (residential egress) — drive a remote browser over CDP.

The strongest egress lever we own: connect to a browser running on a *residential*
IP with a real, full Chrome fingerprint, instead of this box's datacenter IP. That
is the one thing no local fingerprint trick can fake, and it's the actual reason
hard Cloudflare / DataDome hosts (and datacenter-blocklisted sites) wall us.

Operator-provided seam: export BU_CDP_URL to a CDP endpoint and this tier connects
to it. Start one with browser-harness:

    start_remote_daemon("scrape", proxyCountryCode="us")   # prints a cdpUrl

then export that URL as BU_CDP_URL. Unset → tier is disabled and the cascade
skips it. It sits after the local browser tiers (only walls that beat them pay
this cost) and before paid Firecrawl.
"""
from __future__ import annotations

import os

from ..concurrency import browser_slot
from ..normalize import html_to_markdown
from ..policy.gates import check

NAME = "tier_6"
PAID = False

# Per-tier navigation timeout (seconds); override with SCRAPER_TIER_6_TIMEOUT_S.
# Default kept at 30s — remote CDP over a residential proxy is slow to first paint.
# Back-compat: honor the pre-0.5.0 SCRAPER_RESIDENTIAL_TIMEOUT_MS (ms) if unset.
_legacy_ms = os.getenv("SCRAPER_RESIDENTIAL_TIMEOUT_MS")
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_6_TIMEOUT_S",
                             str(float(_legacy_ms) / 1000) if _legacy_ms else "30"))
_TIMEOUT_MS = int(_TIMEOUT_S * 1000)


def disabled() -> bool:
    """Off unless an operator has wired a residential CDP endpoint."""
    return not os.getenv("BU_CDP_URL")


def fetch(url: str) -> str:
    from patchright.sync_api import sync_playwright
    cdp = os.environ["BU_CDP_URL"]
    with browser_slot(NAME), sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp)
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, timeout=_TIMEOUT_MS, wait_until="domcontentloaded")
                if len(page.content()) < 5000:
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                html = page.content()
            finally:
                page.close()
            md = html_to_markdown(html, base_url=page.url or url)
        finally:
            browser.close()
    return check(url, md)
