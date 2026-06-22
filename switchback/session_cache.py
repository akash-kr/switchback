"""Cookie provisioning for the HTTP/browser tiers — two sources, merged per hit.

1. Session cache (cf_clearance reuse). After Tier 2 solves a Cloudflare
   challenge its ``cf_*`` cookies are cached per ``(host, egress_scope)`` and
   replayed on later hits so the ~5s solve is skipped. cf_clearance is bound to
   the UA that solved it and the egress IP — hence the scope key and the stored
   UA. Entries expire after ``SCRAPER_SESSION_TTL_S`` (cf_clearance's typical
   lifetime); a re-detected wall calls ``forget()`` so a stale cookie self-heals
   into a fresh solve. Disable with ``SCRAPER_DISABLE_SESSION_CACHE=1``.

2. Auth import (``SCRAPER_COOKIES_FILE``). A Netscape ``cookies.txt`` the user
   exports from a logged-in browser; domain-matching cookies are sent so the
   tiers can fetch pages behind a login. Opt-in; unset/absent → no-op.

The cf cache layers on top of auth cookies for the same request. Reads are in
memory; writes are write-through to ``state/session_cache.json`` (atomic).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import threading
import time
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlsplit

from . import egress
from .policy.gates import host_of

logger = logging.getLogger(__name__)

_DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
_STATE_DIR = os.getenv("SCRAPER_STATE_DIR", _DEFAULT_STATE_DIR)
CACHE_PATH = os.path.join(_STATE_DIR, "session_cache.json")

_TTL_S = float(os.getenv("SCRAPER_SESSION_TTL_S", "1800"))

# cf_clearance is the durable one; __cf_bm / cf_chl_* are the supporting set.
def _is_cf_cookie(name: str) -> bool:
    return name == "cf_clearance" or name.startswith("__cf") or name.startswith("cf_chl")


def _enabled() -> bool:
    return os.getenv("SCRAPER_DISABLE_SESSION_CACHE") not in ("1", "true", "True")


# ── cf_clearance session cache ────────────────────────────────────────────────

_LOCK = threading.Lock()
_DB: dict | None = None  # {"version": 1, "entries": {"<host>\t<scope>": {...}}}


def _load() -> dict:
    global _DB
    if _DB is not None:
        return _DB
    db = {"version": 1, "entries": {}}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                db = json.load(f)
            db.setdefault("entries", {})
        except Exception as e:
            logger.error(f"session_cache: load failed ({e}); starting fresh")
    _DB = db
    return _DB


def _save(db: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = f"{CACHE_PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def _key(url: str) -> str:
    return f"{host_of(url)}\t{egress.scope_label()}"


def _cache_cookies(url: str) -> dict:
    """Fresh cached cf cookies for this host+scope, or {} (also lazily evicts
    expired entries)."""
    if not _enabled():
        return {}
    with _LOCK:
        db = _load()
        key = _key(url)
        ent = db["entries"].get(key)
        if not ent:
            return {}
        if time.time() - ent.get("ts", 0) > _TTL_S:
            db["entries"].pop(key, None)
            _save(db)
            return {}
        return dict(ent.get("cookies", {}))


def remember(url: str, cookies: dict, ua: str = "") -> None:
    """Persist the cf cookies from a successful solve for this host+scope."""
    if not _enabled():
        return
    cf = {k: v for k, v in (cookies or {}).items() if _is_cf_cookie(k)}
    if not cf:
        return
    with _LOCK:
        db = _load()
        db["entries"][_key(url)] = {"cookies": cf, "ua": ua, "ts": time.time()}
        _save(db)


def forget(url: str) -> None:
    """Drop the cached entry for this host+scope (a re-detected wall means the
    cookie is stale or IP-mismatched)."""
    if not _enabled():
        return
    with _LOCK:
        db = _load()
        if db["entries"].pop(_key(url), None) is not None:
            _save(db)


# ── auth cookie import (SCRAPER_COOKIES_FILE) ────────────────────────────────

_AUTH_LOCK = threading.Lock()
_AUTH_JAR: MozillaCookieJar | None = None
_AUTH_LOADED = False


def _auth_jar() -> MozillaCookieJar | None:
    global _AUTH_JAR, _AUTH_LOADED
    if _AUTH_LOADED:
        return _AUTH_JAR
    with _AUTH_LOCK:
        if _AUTH_LOADED:
            return _AUTH_JAR
        path = os.getenv("SCRAPER_COOKIES_FILE")
        if path and os.path.exists(path):
            jar = MozillaCookieJar()
            try:
                jar.load(path, ignore_discard=True, ignore_expires=True)
                _AUTH_JAR = jar
                logger.info(f"session_cache: loaded auth cookies from {path}")
            except Exception as e:
                logger.error(f"session_cache: cookie import failed ({e})")
        _AUTH_LOADED = True
    return _AUTH_JAR


def _host_matches(cookie_domain: str, host: str) -> bool:
    d = cookie_domain.lstrip(".")
    return host == d or host.endswith("." + d)


def _auth_cookies(url: str) -> dict:
    jar = _auth_jar()
    if not jar:
        return {}
    host = host_of(url)
    return {c.name: c.value for c in jar if _host_matches(c.domain, host)}


# ── logged-in session refresh (SCRAPER_LOGIN_HOOK) ───────────────────────────
#
# A cookies.txt export goes stale. Configure SCRAPER_LOGIN_HOOK="pkg.module:func"
# — a callable func(host) -> {cookie_name: value}. When an authed host trips a
# login / bot wall, the engine calls the hook once, persists the returned cookies
# per host (in the session cache under "logins"), and overlays them on the
# cookies.txt jar for every later request and run. The hook owns the site-specific
# mechanics (drive a browser, hit an auth API, read a secret); the engine stays
# generic, which is what lets it cover many/varied logged-in sites.

_LOGIN_LOCK = threading.Lock()
_LOGIN_HOOK = None
_LOGIN_HOOK_LOADED = False


def _login_hook():
    global _LOGIN_HOOK, _LOGIN_HOOK_LOADED
    if _LOGIN_HOOK_LOADED:
        return _LOGIN_HOOK
    with _LOGIN_LOCK:
        if not _LOGIN_HOOK_LOADED:
            spec = os.getenv("SCRAPER_LOGIN_HOOK", "")
            if spec and ":" in spec:
                mod, _, fn = spec.partition(":")
                try:
                    _LOGIN_HOOK = getattr(importlib.import_module(mod), fn)
                    logger.info(f"session_cache: login hook loaded ({spec})")
                except Exception as e:
                    logger.error(f"session_cache: login hook {spec!r} load failed: {e}")
            _LOGIN_HOOK_LOADED = True
    return _LOGIN_HOOK


def has_login_hook() -> bool:
    return _login_hook() is not None


def _login_cookies(url: str) -> dict:
    """Refreshed login cookies stored for this host (host-level, scope-agnostic)."""
    with _LOCK:
        db = _load()
        ent = db.get("logins", {}).get(host_of(url))
    return dict(ent.get("cookies", {})) if ent else {}


def is_authed_host(url: str) -> bool:
    """True when we hold a logged-in credential for this host — an imported
    cookies.txt match or previously-refreshed login cookies. Lets the policy tell
    a dead session (worth re-logging-in for) from a plain bot wall."""
    return bool(_auth_cookies(url) or _login_cookies(url))


def refresh_login(url: str) -> bool:
    """Invoke the configured login hook for this host and persist the cookies it
    returns. True if fresh cookies were obtained; no-op without a hook."""
    hook = _login_hook()
    if not hook:
        return False
    host = host_of(url)
    try:
        cookies = hook(host) or {}
    except Exception as e:
        logger.error(f"session_cache: login hook failed for {host}: {e}")
        return False
    if not cookies:
        logger.warning(f"session_cache: login hook returned no cookies for {host}")
        return False
    with _LOCK:
        db = _load()
        db.setdefault("logins", {})[host] = {"cookies": dict(cookies), "ts": time.time()}
        _save(db)
    logger.info(f"session_cache: refreshed login for {host} ({len(cookies)} cookies)")
    return True


# ── public API used by the tiers ─────────────────────────────────────────────

def cookies_for(url: str, *, include_cache: bool) -> dict:
    """Cookies to send for this URL: imported auth, overlaid by refreshed login
    cookies, plus (when requested) the cached cf_clearance. Freshest wins on a
    name clash (auth file < refreshed login < cf cache)."""
    out = _auth_cookies(url)
    out.update(_login_cookies(url))
    if include_cache:
        out.update(_cache_cookies(url))
    return out


def cookie_header(url: str, *, include_cache: bool) -> str | None:
    """The same cookies as a ``Cookie:`` header value (for curl_cffi, which has
    no cookies= kwarg), or None when there are none."""
    c = cookies_for(url, include_cache=include_cache)
    return "; ".join(f"{k}={v}" for k, v in c.items()) if c else None


def browser_cookies(url: str | None = None) -> list[dict]:
    """Auth + refreshed-login cookies as Playwright ``add_cookies`` records.
    cf_clearance is not replayed into browsers — they solve natively. Refreshed
    login cookies are added for the URL's host when a URL is given."""
    out = []
    jar = _auth_jar()
    if jar:
        out.extend({"name": c.name, "value": c.value,
                    "domain": c.domain, "path": c.path or "/"} for c in jar)
    if url:
        host = host_of(url)
        out.extend({"name": k, "value": v, "domain": host, "path": "/"}
                   for k, v in _login_cookies(url).items())
    return out
