"""Tier 3 — stealth headless browser (patchright).

Renders JS-heavy SPAs. patchright is a hardened Playwright fork that evades the
common automation fingerprints. Tries domcontentloaded first, then networkidle
if the DOM is suspiciously small (lazy/JS content).

Future: (a) batch many URLs through one browser (musings does this — big perf
win); (b) browser-harness mode to drive the user's logged-in Chrome over CDP for
auth-walled pages (set BU_CDP_URL).
"""
from __future__ import annotations

from . import _browser
from .. import session_cache, session_trace
from ..concurrency import browser_slot
from ..egress import playwright_proxy, add_wire_bytes
from ..normalize import html_to_markdown
from ..policy.gates import check

NAME = "tier3_browser"
PAID = False


def fetch(url: str, timeout_ms: int = 15000) -> str:
    from patchright.sync_api import sync_playwright
    with browser_slot(NAME), sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=playwright_proxy())
        ctx = None
        try:
            # No user_agent override: patchright ships a real, internally
            # consistent Chromium fingerprint; overriding the UA desyncs it from
            # the engine version / client hints and defeats the stealth fork.
            ctx = browser.new_context()
            session_trace.start(ctx, url)
            auth = session_cache.browser_cookies(url)
            if auth:
                ctx.add_cookies(auth)
            page = ctx.new_page()
            responses: list = []
            page.on("response", lambda resp: responses.append(resp))
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            html = page.content()
            if len(html) < 5000:
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                html = page.content()
            # A JS bot-manager (Akamai/Imperva/…) may serve a sensor interstitial
            # first; settle + reload once to get the real page on an acceptable IP.
            if _browser.looks_blocked(html, page.url or url):
                html = _browser.reload_through_challenge(page, url, timeout_ms)
            add_wire_bytes(_browser.response_bytes(responses))
            md = html_to_markdown(html, base_url=page.url or url)
        finally:
            if ctx is not None:
                session_trace.stop(ctx, url)
            browser.close()
    return check(url, md)
