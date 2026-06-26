"""NYT login hook for the session/cookie integration suite (GLD-1).

A ``SCRAPER_LOGIN_HOOK`` target: ``func(host) -> {cookie_name: value}``. Wire it
with::

    export SCRAPER_LOGIN_HOOK="tests.nyt_login:login"

When an authed NYT host trips a login/bot wall, the engine calls this once,
persists the returned cookies per host, overlays them on every tier, and re-runs
on a fresh budget (see switchback.session_cache + orchestrator._run_one).

Credentials come from the environment (``NYT_USERNAME`` / ``NYT_PASSWORD``),
loaded from a gitignored ``.env`` by the test fixtures — never hard-coded here.

The login is driven with patchright (the same stealth Chromium the browser tiers
use) so the login page itself isn't bot-flagged. The selectors target NYT's
current two-step email→password login; if NYT changes its form, update them here.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

LOGIN_URL = "https://myaccount.nytimes.com/auth/login"

# Cookies that actually carry the NYT subscriber session. We keep only these so
# the engine overlays a tight, meaningful set rather than every tracking cookie.
_SESSION_COOKIE_NAMES = {"NYT-S", "NYT-MPS", "nyt-a", "SID", "NYT-Edition"}


def _credentials() -> tuple[str, str] | None:
    user = os.getenv("NYT_USERNAME")
    pw = os.getenv("NYT_PASSWORD")
    if not user or not pw:
        return None
    return user, pw


def login(host: str) -> dict:
    """Log into NYT and return the subscriber session cookies for ``host``.

    Returns ``{}`` (a no-op for the engine) when creds are absent or the login
    flow can't complete — the caller logs it and moves on.
    """
    creds = _credentials()
    if not creds:
        logger.warning("nyt_login: NYT_USERNAME/NYT_PASSWORD not set — skipping login")
        return {}
    username, password = creds

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        logger.error("nyt_login: patchright not installed — `pip install -e \".[browser]\"`")
        return {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)

            # Step 1 — email.
            page.fill('input[name="email"]', username)
            page.click('button[data-testid="submit-email"], button[type="submit"]')

            # Step 2 — password (NYT reveals it after the email step).
            page.wait_for_selector('input[name="password"]', timeout=30_000)
            page.fill('input[name="password"]', password)
            page.click('button[data-testid="login-button"], button[type="submit"]')

            # Settled when the session cookie lands.
            page.wait_for_timeout(5_000)

            cookies = {
                c["name"]: c["value"]
                for c in ctx.cookies()
                if c["name"] in _SESSION_COOKIE_NAMES
            }
        finally:
            browser.close()

    if not cookies:
        logger.warning("nyt_login: login produced no session cookies "
                       "(creds wrong, MFA, or selectors stale?)")
        return {}

    logger.info(f"nyt_login: obtained {len(cookies)} session cookies for {host}")
    return cookies
