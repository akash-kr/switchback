"""Quality gates — minimum acceptable content length per host.

A page that renders to a few hundred chars of nav is a failure, not a success;
the gate makes a tier "fall through" instead of returning junk.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

MIN_MD_LEN = 2000  # default floor

# Hosts whose articles are legitimately short (API stubs, curated explainers).
MIN_MD_LEN_PER_HOST = {
    "arxiv.org": 500,
    "export.arxiv.org": 500,
    "en.wikipedia.org": 1000,
    "www.metmuseum.org": 500,
}


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def min_len_for(url: str) -> int:
    return MIN_MD_LEN_PER_HOST.get(host_of(url), MIN_MD_LEN)


# High-precision bot-wall / block-page markers, each tagged with the vendor that
# serves it. A page can clear the length gate yet be a Cloudflare "Just a
# moment..." interstitial (long but worthless), so the length floor alone isn't
# enough. These are scanned ONLY in the head of the content (the title /
# first-heading region) — a long article that merely mentions one of these
# phrases in its body won't trip the gate. Keep this list narrow: a false
# positive (rejecting a real page) is worse than missing an exotic wall.
#
# The vendor tag is what lets the policy *learn which wall* a host serves
# (recorded per host in botwall_db.json), so dashboards can show challenges by
# domain and routing can adapt. Order matters: the first match wins, so put the
# vendor-specific phrases before the generic ones.
_BOTWALL_MARKERS = (
    ("just a moment...",                    "cloudflare"),
    ("checking your browser",               "cloudflare"),
    ("attention required! | cloudflare",    "cloudflare"),
    ("verifying you are human",             "cloudflare"),  # Turnstile newer copy
    ("verify you are human",                "cloudflare"),
    ("enable javascript and cookies to continue", "cloudflare"),
    ("request unsuccessful. incapsula",     "incapsula"),   # Imperva Incapsula
    ("pardon our interruption",             "perimeterx"),  # PerimeterX / HUMAN
    ("press & hold",                        "perimeterx"),  # PerimeterX challenge
    ("humans only",                         "datadome"),    # DataDome (e.g. Glassdoor)
    ("access denied",                       "akamai"),      # Akamai / generic 403
    ("unusual traffic from your computer",  "google"),      # Google bot interstitial
    ("are you a human",                     "generic"),
    ("ddos protection by",                  "generic"),     # generic CDN challenge
)
_BOTWALL_HEAD_CHARS = 600


def classify_botwall(md: str | None) -> str | None:
    """Return the vendor of the bot-wall in the head of `md` (cloudflare /
    incapsula / perimeterx / datadome / akamai / google / generic), or None if
    the content doesn't look like a wall. First marker match wins."""
    if not md:
        return None
    head = md[:_BOTWALL_HEAD_CHARS].lower()
    for marker, vendor in _BOTWALL_MARKERS:
        if marker in head:
            return vendor
    return None


def _looks_like_botwall(md: str) -> bool:
    return classify_botwall(md) is not None


# A Cloudflare *JS challenge* specifically — the thing cloudscraper (Tier 2) can
# actually solve. Distinct from a generic block: a Cloudflare WAF 1020 / DataDome
# / origin 403 is served-by-CF-or-not but un-solvable, so it must NOT match here.
_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "enable javascript and cookies to continue",
)


def is_cf_challenge(headers, body: str | None) -> bool:
    """True when an (often 403/503) response is a Cloudflare JS challenge that
    Tier 2 can clear — served by Cloudflare AND carrying a challenge signal."""
    h = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    by_cf = h.get("server", "").lower() == "cloudflare" or "cf-ray" in h
    if not by_cf:
        return False
    if h.get("cf-mitigated", "").lower() == "challenge":
        return True
    head = (body or "")[:_BOTWALL_HEAD_CHARS].lower()
    return any(m in head for m in _CF_CHALLENGE_MARKERS)


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an exception: a response object if the
    library attached one (requests/cloudscraper/curl_cffi), else the first 4xx/5xx
    found in the message (curl_cffi/urllib render it as text, e.g. 'HTTP Error 403')."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b([45]\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map a raised tier exception to (error_class, status_code).

    error_class ∈ {http_block, rate_limited, timeout, connection, http_error,
    error}. This is what lets the policy treat a hard 403/401 (datacenter-IP /
    UA block) or a 429 as egress-worthy — the cheaper tiers raise these instead
    of returning a marker page, so without this they'd never escalate."""
    status = _status_of(exc)
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if status in (401, 403):
        return "http_block", status
    if status == 429:
        return "rate_limited", status
    if "timeout" in name or "timed out" in msg or "timeout" in msg:
        return "timeout", status
    if any(s in msg for s in (
            "could not resolve", "name or service not known", "getaddrinfo",
            "connection refused", "connection reset", "failed to connect",
            "ssl", "certificate")):
        return "connection", status
    if status and 400 <= status < 600:
        return "http_error", status
    return "error", status


def check(url: str, md: str | None) -> str:
    """Return md if it clears the gates, else raise BotWall / ShortContent."""
    vendor = classify_botwall(md)
    if vendor:
        raise BotWall(f"bot-wall / block page detected ({vendor})", vendor=vendor)
    gate = min_len_for(url)
    n = len(md) if md else 0
    if n < gate:
        raise ShortContent(f"body too short: {n} < {gate}")
    return md


class ShortContent(RuntimeError):
    """Content fetched but below the quality gate — treated as a tier miss."""


class BotWall(RuntimeError):
    """Content fetched but it's a bot-wall / block interstitial (e.g. Cloudflare
    "Just a moment...") rather than the real page — treated as a tier miss so the
    cascade falls through to a stealthier tier. `vendor` names the wall
    (cloudflare / datadome / akamai / …) when known, so the policy can learn
    which challenge a host serves."""

    def __init__(self, *args, vendor: str | None = None):
        super().__init__(*args)
        self.vendor = vendor


class RateLimited(RuntimeError):
    """Tier hit an upstream rate/quota limit (e.g. HTTP 429) — traced distinctly
    from a normal failure so limit-pressure is visible in the dashboard."""
