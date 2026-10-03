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

import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlparse

from ..normalize import html_to_markdown
from ..policy.gates import _looks_like_botwall

logger = logging.getLogger(__name__)

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


# --- Opt-in private-network guard (SCRAPER_BROWSER_BLOCK_PRIVATE=1) -------------
# A scraped page can point the browser at a non-public address: a redirect, a
# frame, an image, a fetch() or a WebSocket. Whatever a service on the scraping
# machine or its network answers then comes back as scraped content. With the
# guard on, tiers 4 and 5 refuse every such request. Off by default: some
# deployments scrape their own intranet on purpose.
#
# Limits: hosts are resolved here, so a host whose DNS answer changes between this
# check and the browser's own lookup, or one only a proxy resolves differently,
# isn't covered; WebRTC isn't intercepted. Tier 6 is a remote browser whose
# network isn't this machine's, so it's left alone.

def block_private_enabled() -> bool:
    return bool(os.getenv("SCRAPER_BROWSER_BLOCK_PRIVATE"))


def context_options() -> dict:
    """Extra new_context()/new_page() options when the guard is on: requests a
    service worker makes would bypass routing, so service workers are blocked."""
    return {"service_workers": "block"} if block_private_enabled() else {}


def is_private_url(url: str, cache: dict | None = None) -> bool:
    """Whether an http(s)/ws(s) URL's host is, or resolves to, a non-public address
    (loopback, private, link-local incl. cloud metadata, CGNAT, multicast...).
    Other schemes (data:, blob:, about:) and hosts that don't resolve aren't judged:
    the browser fails those on its own."""
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not host:
        return False
    if cache is not None and host in cache:
        return cache[host]
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        except (OSError, UnicodeError):
            addresses = []
    verdict = any(_non_public(address) for address in addresses)
    if cache is not None:
        cache[host] = verdict
    return verdict


def _non_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def block_private_networks(context) -> bool:
    """Install the guard on a browser context when opted in; returns whether it did.
    A no-op (nothing installed, nothing changed) when SCRAPER_BROWSER_BLOCK_PRIVATE
    is unset."""
    if not block_private_enabled():
        return False
    verdicts: dict[str, bool] = {}

    def guard_request(route):
        url = route.request.url
        if is_private_url(url, verdicts):
            logger.info(f"browser: blocked a request to a non-public address ({urlparse(url).hostname})")
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def guard_socket(ws):
        if is_private_url(ws.url, verdicts):
            logger.info(f"browser: blocked a WebSocket to a non-public address ({urlparse(ws.url).hostname})")
            ws.close()
        else:
            ws.connect_to_server()

    context.route("**/*", guard_request)
    if hasattr(context, "route_web_socket"):  # Playwright >= 1.48
        context.route_web_socket(re.compile(r".*"), guard_socket)
    return True
