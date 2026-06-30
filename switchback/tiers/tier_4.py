"""Tier 3 — stealth headless browser (patchright).

Renders JS-heavy SPAs. patchright is a hardened Playwright fork that evades the
common automation fingerprints. Tries domcontentloaded first, then networkidle
if the DOM is suspiciously small (lazy/JS content).

Future: (a) batch many URLs through one browser (musings does this — big perf
win); (b) browser-harness mode to drive the user's logged-in Chrome over CDP for
auth-walled pages (set BU_CDP_URL).
"""
from __future__ import annotations

import os

from . import _browser
from .. import session_cache, session_trace
from ..concurrency import browser_slot
from ..egress import playwright_proxy, add_wire_bytes
from ..normalize import html_to_markdown
from ..policy.gates import Unavailable, check

NAME = "tier_4"
PAID = False

# Per-tier navigation timeout (seconds); override with SCRAPER_TIER_4_TIMEOUT_S.
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_4_TIMEOUT_S", "15"))

# Install hint surfaced when patchright or its Chromium isn't ready — notably
# during an async cold-start install (the browser binary lands after boot).
_INSTALL_HINT = 'pip install "switchback[browser]" && patchright install chromium'


def available() -> tuple[bool, str]:
    """Whether patchright is importable *and* its Chromium is downloaded.
    Returns (ok, detail). On a cold start where the browser is installed by a
    background thread, this flips to True once that finishes. Used by `fetch`
    (clear `unavailable` reason instead of a buried launch error) and by
    `switchback doctor`."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        return False, f"patchright not installed — {_INSTALL_HINT}"
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
    except Exception as e:  # pragma: no cover — driver start is environment-specific
        return False, f"patchright driver error: {e}"
    if not exe or not os.path.exists(exe):
        return False, f"patchright Chromium not installed — {_INSTALL_HINT}"
    return True, "patchright + Chromium ready"


def fetch(url: str, timeout_ms: int = int(_TIMEOUT_S * 1000)) -> str:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise Unavailable(f"patchright not installed — {_INSTALL_HINT}")
    with browser_slot(NAME), sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, proxy=playwright_proxy())
        except Exception as e:
            # Chromium not downloaded yet (cold-start window) reads as a launch
            # error; surface it as unavailable + the fix, not a generic failure.
            msg = str(e)
            if "Executable doesn't exist" in msg or "patchright install" in msg:
                raise Unavailable(
                    f"patchright Chromium not installed — {_INSTALL_HINT}")
            raise
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
