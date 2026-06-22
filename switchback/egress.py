"""Egress configuration — proxy wiring shared across tiers.

Two optional env vars:

  SCRAPER_PROXY         applied to *every* request (all tiers, all URLs).
  SCRAPER_EGRESS_PROXY  applied only while a request is in the "egress scope" —
                        i.e. for a host the policy flagged needs_egress. This is
                        the cost-scoped lever: the easy majority that already
                        succeeds free at the datacenter IP stays direct, and only
                        the hard, walled hosts spend the (often metered)
                        residential proxy bandwidth.

The orchestrator opens an egress scope around the cascade for needs_egress hosts
(see ``egress_scope``); the per-tier helpers resolve the right proxy for the
current scope. Both shapes are returned — the requests/curl_cffi ``proxies`` dict
and the Playwright/Camoufox ``proxy`` dict. Nothing set / not in scope → None and
the tier runs on the direct connection.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

# Per-thread scope flag. Threading-safe by construction: each worker thread in a
# ThreadPoolExecutor gets its own instance (default unset → False), and the
# orchestrator sets/reads it within the same thread that runs the tier fetch.
_scope = threading.local()


@contextmanager
def egress_scope(enabled: bool):
    """Mark the enclosed work as egress-scoped (per-thread). While enabled and
    SCRAPER_EGRESS_PROXY is set, the proxy helpers return that proxy. Always
    restores the previous scope on exit, including on early return/raise."""
    prev = getattr(_scope, "egress", False)
    _scope.egress = bool(enabled)
    try:
        yield
    finally:
        _scope.egress = prev


def add_wire_bytes(n: int) -> None:
    """Tally bytes actually transferred over the network for the current URL's
    cascade (per-thread). HTTP tiers add the response size; browser tiers sum each
    loaded resource. The orchestrator reads this to cost residential bandwidth on
    real wire bytes, not the cleaned markdown."""
    _scope.wire_bytes = getattr(_scope, "wire_bytes", 0) + int(n)


def take_wire_bytes() -> int:
    """Return and reset the per-thread wire-byte tally (call once per URL)."""
    n = getattr(_scope, "wire_bytes", 0)
    _scope.wire_bytes = 0
    return n


def has_egress_proxy() -> bool:
    """True when a residential/escalation proxy is configured."""
    return bool(os.getenv("SCRAPER_EGRESS_PROXY"))


def in_egress_scope() -> bool:
    """Raw per-thread egress flag. Lets a tier that offloads its blocking call to
    a worker thread re-apply the scope there (thread-locals don't inherit)."""
    return getattr(_scope, "egress", False)


def scope_label() -> str:
    """'egress' when the request routes through the residential egress proxy,
    else 'direct'. cf_clearance is IP-bound, so the session cache keys on this
    to never replay a cookie across the direct/proxy boundary."""
    if getattr(_scope, "egress", False) and os.getenv("SCRAPER_EGRESS_PROXY"):
        return "egress"
    return "direct"


def _active_proxy_url() -> str | None:
    """The proxy URL for the current scope: the egress proxy when in scope and
    set, otherwise the global proxy (or None)."""
    if getattr(_scope, "egress", False) and os.getenv("SCRAPER_EGRESS_PROXY"):
        return os.getenv("SCRAPER_EGRESS_PROXY")
    return os.getenv("SCRAPER_PROXY") or None


def requests_proxies() -> dict | None:
    """For curl_cffi / requests / cloudscraper: {"http": url, "https": url}."""
    url = _active_proxy_url()
    return {"http": url, "https": url} if url else None


def playwright_proxy() -> dict | None:
    """For patchright / camoufox: {"server", "username"?, "password"?}."""
    url = _active_proxy_url()
    if not url:
        return None
    parts = urlsplit(url)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    cfg: dict[str, str] = {"server": server}
    if parts.username:
        cfg["username"] = parts.username
    if parts.password:
        cfg["password"] = parts.password
    return cfg
