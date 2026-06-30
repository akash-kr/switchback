"""Tier 1 — plain HTTP with TLS fingerprint impersonation.

curl_cffi impersonates a real Chrome TLS handshake, which clears many naive bot
walls without a browser. Handles PDFs inline. Fast and cheap.

The bare "chrome" alias resolves to an old default; we pin recent targets and
rotate them deterministically per host, so our traffic isn't one shared JA3 yet
each host stays reproducible (and pairs cleanly with the session cache, which
records the target that won).

No User-Agent override: the impersonate target already sends a UA that matches
its TLS fingerprint. Overriding it with a stale string is a detection tell (TLS
says one Chrome version, the header says another).
"""
from __future__ import annotations

import hashlib
import os
from urllib.parse import urlsplit

from .. import session_cache
from ..egress import requests_proxies, add_wire_bytes
from ..normalize import html_to_markdown, pdf_bytes_to_text
from ..policy.gates import BotWall, check, is_cf_challenge

NAME = "tier_2"
PAID = False

# Per-tier request timeout (seconds); override with SCRAPER_TIER_2_TIMEOUT_S.
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_2_TIMEOUT_S", "15"))

# Recent Chrome JA3 targets available in curl_cffi 0.15.x. A small spread of real
# versions mirrors how live traffic is distributed across Chrome releases.
_IMPERSONATE_TARGETS = ("chrome131", "chrome136", "chrome142")


def _impersonate_for(url: str) -> str:
    host = urlsplit(url).hostname or ""
    h = int(hashlib.sha1(host.encode()).hexdigest(), 16)
    return _IMPERSONATE_TARGETS[h % len(_IMPERSONATE_TARGETS)]


def fetch(url: str) -> str:
    from curl_cffi import requests as cffi
    # Auth cookies only: the cached cf_clearance is UA-bound to whichever tier
    # solved it, and CF hosts route straight to Tier 2 on repeat, so replaying it
    # against Tier 1's distinct impersonate UA would be a mismatch tell.
    cookie = session_cache.cookie_header(url, include_cache=False)
    headers = {"Cookie": cookie} if cookie else None
    r = cffi.get(url, timeout=_TIMEOUT_S, allow_redirects=True,
                 impersonate=_impersonate_for(url),
                 proxies=requests_proxies(), headers=headers)
    add_wire_bytes(len(r.content))  # count even on a block — failed fetches burn bandwidth too
    if r.status_code >= 400:
        # A Cloudflare JS challenge often returns 403/503 with the interstitial in
        # the body. Surface that as a botwall (Tier 2 can solve it) rather than a
        # hard http_block — which the orchestrator uses to skip Tier 2 entirely.
        if is_cf_challenge(r.headers, r.text):
            raise BotWall("cloudflare challenge", vendor="cloudflare")
        r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    is_pdf = "application/pdf" in ctype or r.url.lower().split("?")[0].endswith(".pdf")
    if is_pdf:
        try:
            text = pdf_bytes_to_text(r.content)
        finally:
            r.close()
        return check(url, text)
    return check(url, html_to_markdown(r.text, base_url=r.url))
