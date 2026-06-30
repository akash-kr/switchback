"""Tier 3b — Camoufox (hardened Firefox), env-gated stealth.

An *orthogonal* fingerprint to the Chromium patchright tier: Camoufox patches
stealth at the C++ level and is best-in-class for **headless** detection evasion,
so it can clear hosts where the Chromium browser still gets blocked. It's the
slowest rung we own (~40s on a hard Cloudflare solve), but it only fires after
the four cheaper tiers AND patchright all miss, so easy traffic never pays for
it. ON by default — opt out with SCRAPER_DISABLE_CAMOUFOX=1.

Needs its Firefox build (`camoufox fetch`); if absent the launch raises and the
cascade falls through to Firecrawl. Tried after the Chromium browser misses,
before the paid Firecrawl tier.
"""
from __future__ import annotations

import logging
import os

from . import _browser
from .. import session_cache, session_trace
from ..concurrency import browser_slot
from ..egress import playwright_proxy, add_wire_bytes
from ..normalize import html_to_markdown
from ..policy.gates import check

logger = logging.getLogger(__name__)

NAME = "tier_5"
PAID = False

# Per-tier navigation timeout (seconds); override with SCRAPER_TIER_5_TIMEOUT_S.
# Default kept at 45s — camoufox is the slowest rung (~40s on a hard CF solve).
# Back-compat: honor the pre-0.5.0 SCRAPER_CAMOUFOX_TIMEOUT_MS (ms) if unset.
_legacy_ms = os.getenv("SCRAPER_CAMOUFOX_TIMEOUT_MS")
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_5_TIMEOUT_S",
                             str(float(_legacy_ms) / 1000) if _legacy_ms else "45"))
_TIMEOUT_MS = int(_TIMEOUT_S * 1000)


def disabled() -> bool:
    """On by default; opt out (heavy + slow) with SCRAPER_DISABLE_CAMOUFOX=1."""
    return bool(os.getenv("SCRAPER_DISABLE_CAMOUFOX"))


def _geoip_available() -> bool:
    """camoufox's geoip matching needs the `geoip2` package (the camoufox[geoip]
    extra). Without it, requesting geoip raises and kills the whole tier — so we
    probe and degrade gracefully instead."""
    try:
        import geoip2  # noqa: F401
        return True
    except Exception:
        return False


def _launch_opts() -> dict:
    """Camoufox evasion knobs. We launch it bare no longer: humanize + randomized
    desktop OS make the fingerprint blend in; with a proxy set we also turn on
    geoip so timezone/locale/geolocation match the proxy's IP (a mismatch there
    is itself a tell) — but only when the geoip extra is installed."""
    opts: dict = {"headless": True, "humanize": True,
                  "os": ["windows", "macos", "linux"]}
    proxy = playwright_proxy()
    if proxy:
        opts["proxy"] = proxy
        if _geoip_available():
            opts["geoip"] = True
        else:
            logger.warning("camoufox: proxy set but geoip extra missing "
                           "(pip install camoufox[geoip]); locale/timezone won't "
                           "match the proxy IP — a possible detection tell")
    return opts


def fetch(url: str) -> str:
    from camoufox.sync_api import Camoufox
    with browser_slot(NAME), Camoufox(**_launch_opts()) as browser:
        page = browser.new_page()
        responses: list = []
        page.on("response", lambda resp: responses.append(resp))
        try:
            session_trace.start(page.context, url)
            auth = session_cache.browser_cookies(url)
            if auth:
                page.context.add_cookies(auth)
            page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
            html = page.content()
            # JS bot-manager sensor interstitial → settle + reload once.
            if _browser.looks_blocked(html, url):
                html = _browser.reload_through_challenge(page, url, _TIMEOUT_MS)
            add_wire_bytes(_browser.response_bytes(responses))
        finally:
            session_trace.stop(page.context, url)
            page.close()
    return check(url, html_to_markdown(html, base_url=url))
