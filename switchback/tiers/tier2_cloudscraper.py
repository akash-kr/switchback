"""Tier 2 — Cloudflare / anti-bot solver (cloudscraper 3.x "Enhanced Edition").

Targets the specific failure the cheaper tiers can't clear: a Cloudflare JS
challenge / "checking your browser" interstitial. Solves it in-process (no
browser), then returns the real page. First hit to a CF host sleeps ~5s.

cloudscraper 3.x clears v1/v2/v3 (JS-VM) challenges and Turnstile, with stealth
on by default (randomized headers, browser quirks, human-like pacing) and
automatic cf_clearance refresh on 403. Pinned to the GitHub Enhanced Edition;
PyPI is frozen at 1.2.71 (v1/v2 only, no stealth) — see pyproject.toml.

On hard CAPTCHA variants with no solver configured this raises and the cascade
falls through to the stealth browser (Tier 3).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading

from .. import egress, session_cache
from ..egress import requests_proxies
from ..normalize import html_to_markdown
from ..policy.gates import check

logger = logging.getLogger(__name__)

NAME = "tier2_cloudscraper"
PAID = False

# Wall-clock cap on the whole solve. cloudscraper 3.x *attempts* interactive
# Turnstile and can loop for minutes on a challenge it can't clear — far past the
# per-request socket timeout. Capping it here lets the cascade fall through to the
# stealth browser (which can handle interactive challenges) instead of burning the
# per-URL deadline. ~25s comfortably covers a real JS/v3 solve (~5-15s).
_TIMEOUT_S = float(os.getenv("SCRAPER_CLOUDSCRAPER_TIMEOUT_S", "25"))

# Stealth pacing. Kept modest: Tier 2 only fires on CF-suspected hosts, and the
# real latency win comes from skipping the solve entirely on repeat hits (session
# cache), not from long inter-request sleeps.
_STEALTH_OPTIONS = {
    "min_delay": 0.5,
    "max_delay": 1.5,
    "human_like_delays": True,
    "randomize_headers": True,
    "browser_quirks": True,
}


_captcha_warned = False


def _captcha_opts() -> dict:
    """Opt-in third-party captcha solver (off by default). When both env vars are
    set, cloudscraper solves Turnstile / reCAPTCHA / hCaptcha on CF hosts in-process
    via the provider (2captcha, capsolver, capmonster, anticaptcha, deathbycaptcha,
    9kw). PAID: the provider bills per solve. cloudscraper resets its solve counter
    on success, so per-solve counts aren't observable here — track spend in the
    provider's own dashboard."""
    provider = os.getenv("SCRAPER_CAPTCHA_PROVIDER")
    api_key = os.getenv("SCRAPER_CAPTCHA_API_KEY")
    if not (provider and api_key):
        return {}
    global _captcha_warned
    if not _captcha_warned:
        logger.warning(f"tier2: captcha solver active (provider={provider}); "
                       "solves are billed by the provider")
        _captcha_warned = True
    return {"captcha": {"provider": provider, "api_key": api_key}}


def _interpreter_opts() -> dict:
    """The v3 JS-VM challenge runs an interpreter. The 3.x default js2py is pure
    Python — slow and prone to stalling on heavy challenges; Node runs them fast
    and reliably. Prefer it when present, else fall back to the default."""
    return {"interpreter": "nodejs"} if shutil.which("node") else {}


def _make_scraper():
    import cloudscraper
    # enable_stealth / auto_refresh_on_403 are on by default in 3.x; we pass the
    # stealth tuning explicitly. No UA override: cloudscraper derives a UA (and
    # matching cipher suite) from the browser dict; a stale override contradicts it.
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False},
        enable_stealth=True,
        stealth_options=_STEALTH_OPTIONS,
        **_interpreter_opts(),
        **_captcha_opts(),
    )


def _fetch(url: str) -> str:
    scraper = _make_scraper()
    # Replay a cached cf_clearance (skips the ~5s solve) plus any auth cookies.
    cookies = session_cache.cookies_for(url, include_cache=True)
    r = scraper.get(url, timeout=20, proxies=requests_proxies(),
                    cookies=cookies or None)
    r.raise_for_status()
    nbytes = len(r.content)
    md = check(url, html_to_markdown(r.text, base_url=r.url))
    # Cleared: cache whatever cf cookies the session now holds for next time.
    session_cache.remember(url, dict(scraper.cookies),
                           ua=scraper.headers.get("User-Agent", ""))
    return md, nbytes


def fetch(url: str) -> str:
    # Run the (blocking, occasionally runaway) solve under a hard wall-clock cap.
    # A daemon worker means an abandoned solve can't block process exit; it dies on
    # its own socket timeout shortly after. Thread-locals don't inherit, so the
    # egress scope is re-applied inside the worker.
    scoped = egress.in_egress_scope()
    box: dict = {}

    def work():
        with egress.egress_scope(scoped):
            try:
                box["md"], box["bytes"] = _fetch(url)
            except BaseException as e:  # noqa: BLE001 — propagated to caller below
                box["err"] = e

    t = threading.Thread(target=work, name="tier2-cloudscraper", daemon=True)
    t.start()
    t.join(_TIMEOUT_S)
    if t.is_alive():
        raise TimeoutError(
            f"cloudscraper exceeded {_TIMEOUT_S}s (unsolvable challenge); "
            "falling through to the stealth browser")
    # Re-attribute the worker's wire bytes here, in the scope-owning thread.
    egress.add_wire_bytes(box.get("bytes", 0))
    if "err" in box:
        raise box["err"]
    return box["md"]
