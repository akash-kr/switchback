"""Shared helpers for the stealth-browser tiers (patchright, camoufox).

Not a tier itself (leading underscore, not in the TIERS registry) — just the
challenge-resolution mechanic both browsers need.

Akamai Bot Manager / Imperva / Kasada serve a JS-*sensor* interstitial on the
first load: it sets cookies (e.g. ak_bmsc, _abck) as the sensor script runs, then
the *real* content is only returned on a re-request. A single settle + reload
after the sensor runs clears them — provided the egress IP is acceptable. (A hard
IP block never validates `_abck` and the page stays an interstitial, so the tier
still falls through; this just stops us snapshotting the interstitial too early on
IPs that would have passed.)
"""
from __future__ import annotations

from ..normalize import html_to_markdown
from ..policy.gates import _looks_like_botwall

_SETTLE_MS = 5000        # let the sensor JS run and set its cookies
_POST_RELOAD_MS = 1500   # let the real content paint after the reload


def looks_blocked(html: str, url: str) -> bool:
    """True when the rendered DOM is a bot-wall / sensor interstitial."""
    return _looks_like_botwall(html_to_markdown(html, base_url=url))


def response_bytes(responses) -> int:
    """Total wire bytes a render pulled across every resource — the residential-cost
    basis. Reads only the Content-Length header: it's non-blocking. (We deliberately
    do NOT call resp.body() — on a stalled response body() blocks with no timeout and
    can freeze the whole render, which is uninterruptible by the cascade deadline.)
    Responses without a Content-Length are skipped, so this slightly undercounts."""
    total = 0
    for resp in responses:
        try:
            cl = resp.headers.get("content-length")
            if cl:
                total += int(cl)
        except Exception:
            pass
    return total


def reload_through_challenge(page, url: str, timeout_ms: int) -> str:
    """Settle so the bot-manager sensor JS runs, reload once, return fresh html."""
    page.wait_for_timeout(_SETTLE_MS)
    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    page.wait_for_timeout(_POST_RELOAD_MS)
    return page.content()
