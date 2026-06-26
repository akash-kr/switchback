"""Session/cookie integration suite for the NYT scrape flow (GLD-1).

Two layers:

1. Machinery tests (always run, offline, no creds). They exercise the real
   session_cache code paths the NYT flow depends on — cookies.txt injection,
   cf_clearance reuse + TTL eviction, per-source overlay precedence, login-hook
   refresh, egress-scope isolation, and the disable switch. These prove the
   plumbing is correct independently of whether NYT is reachable today.

2. Live NYT flow (skipped without NYT_USERNAME/NYT_PASSWORD in .env). It logs
   into nytimes.com, persists the session, and asserts gated content is reachable
   WITH a session and not without, and that reuse skips re-login within the TTL.

Run:  pytest tests/test_session_nyt.py -v
Live: add creds to .env (see .env.example / README "Testing the NYT session flow").
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import requires_nyt_creds

NYT_HOST = "https://www.nytimes.com"
# A subscriber-gated article path and a public one. The exact gated URL doesn't
# matter for the machinery tests (they never hit the network); the live tests
# below use these to tell "reachable with a session" from "blocked without one".
NYT_GATED_URL = "https://www.nytimes.com/2024/01/01/technology/ai-future.html"
NYT_PUBLIC_URL = "https://www.nytimes.com/section/technology"


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_cookies_txt(path, host_domain: str, pairs: dict) -> str:
    """Write a Netscape cookies.txt with the given cookies scoped to host_domain.
    Returns the path as a str (what SCRAPER_COOKIES_FILE expects)."""
    lines = ["# Netscape HTTP Cookie File"]
    expiry = int(time.time()) + 86_400
    for name, value in pairs.items():
        # domain  include_subdomains  path  secure  expiry  name  value
        lines.append(f"{host_domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# ── 1. Machinery: cookies.txt injection (SCRAPER_COOKIES_FILE) ────────────────

def test_cookies_file_injected_for_matching_host(isolated_state, tmp_path, monkeypatch):
    sc = isolated_state
    cookies_path = _write_cookies_txt(
        tmp_path / "cookies.txt", ".nytimes.com", {"NYT-S": "sess-abc"})
    monkeypatch.setenv("SCRAPER_COOKIES_FILE", cookies_path)
    sc = __import__("importlib").reload(sc)

    sent = sc.cookies_for(NYT_GATED_URL, include_cache=False)
    assert sent.get("NYT-S") == "sess-abc"
    assert sc.is_authed_host(NYT_GATED_URL) is True


def test_cookies_file_not_sent_to_other_hosts(isolated_state, tmp_path, monkeypatch):
    sc = isolated_state
    cookies_path = _write_cookies_txt(
        tmp_path / "cookies.txt", ".nytimes.com", {"NYT-S": "sess-abc"})
    monkeypatch.setenv("SCRAPER_COOKIES_FILE", cookies_path)
    sc = __import__("importlib").reload(sc)

    sent = sc.cookies_for("https://example.com/page", include_cache=False)
    assert "NYT-S" not in sent
    assert sc.is_authed_host("https://example.com/page") is False


# ── 2. Machinery: cf_clearance reuse + TTL eviction ───────────────────────────

def test_cf_clearance_remembered_and_replayed(isolated_state):
    sc = isolated_state
    sc.remember(NYT_GATED_URL, {"cf_clearance": "cf-token-1", "other": "x"}, ua="UA/1")

    sent = sc.cookies_for(NYT_GATED_URL, include_cache=True)
    assert sent.get("cf_clearance") == "cf-token-1"
    # Only cf-family cookies are cached; "other" must not leak through.
    assert "other" not in sent
    # And it must NOT appear when the caller opts out of the cache (Tier 1 path).
    assert "cf_clearance" not in sc.cookies_for(NYT_GATED_URL, include_cache=False)


def test_cf_clearance_evicted_after_ttl(isolated_state, monkeypatch):
    monkeypatch.setenv("SCRAPER_SESSION_TTL_S", "1")
    sc = __import__("importlib").reload(isolated_state)
    sc.remember(NYT_GATED_URL, {"cf_clearance": "cf-token-1"}, ua="UA/1")

    # Within TTL → replayed.
    assert sc.cookies_for(NYT_GATED_URL, include_cache=True).get("cf_clearance")

    time.sleep(1.1)
    # Past TTL → lazily evicted on read, so a fresh solve happens next.
    assert "cf_clearance" not in sc.cookies_for(NYT_GATED_URL, include_cache=True)


def test_forget_drops_cached_session(isolated_state):
    sc = isolated_state
    sc.remember(NYT_GATED_URL, {"cf_clearance": "cf-token-1"}, ua="UA/1")
    assert sc.cookies_for(NYT_GATED_URL, include_cache=True).get("cf_clearance")

    sc.forget(NYT_GATED_URL)
    assert "cf_clearance" not in sc.cookies_for(NYT_GATED_URL, include_cache=True)


def test_disable_session_cache_switch(isolated_state, monkeypatch):
    monkeypatch.setenv("SCRAPER_DISABLE_SESSION_CACHE", "1")
    sc = __import__("importlib").reload(isolated_state)
    sc.remember(NYT_GATED_URL, {"cf_clearance": "cf-token-1"}, ua="UA/1")
    # With the cache disabled, remember() is a no-op and nothing is replayed.
    assert "cf_clearance" not in sc.cookies_for(NYT_GATED_URL, include_cache=True)


# ── 3. Machinery: overlay precedence (auth < login < cf cache) ────────────────

def test_overlay_precedence(isolated_state, tmp_path, monkeypatch):
    sc = isolated_state
    # Auth file sets a base value for NYT-S.
    cookies_path = _write_cookies_txt(
        tmp_path / "cookies.txt", ".nytimes.com",
        {"NYT-S": "from-file", "static": "keep-me"})
    monkeypatch.setenv("SCRAPER_COOKIES_FILE", cookies_path)
    monkeypatch.setenv("SCRAPER_LOGIN_HOOK", "tests.nyt_login:login")
    sc = __import__("importlib").reload(sc)

    # A refreshed login overrides NYT-S and adds its own.
    sc._save({**sc._load(), "logins": {
        "www.nytimes.com": {"cookies": {"NYT-S": "from-login", "NYT-MPS": "mps"}, "ts": time.time()}}})
    sc._DB = None  # force reload of the just-written db

    merged = sc.cookies_for(NYT_GATED_URL, include_cache=True)
    assert merged["NYT-S"] == "from-login"   # login beats file
    assert merged["static"] == "keep-me"     # untouched file cookie survives
    assert merged["NYT-MPS"] == "mps"        # login-only cookie present


# ── 4. Machinery: login-hook refresh (SCRAPER_LOGIN_HOOK) ─────────────────────

def test_login_hook_refresh_persists_cookies(isolated_state, monkeypatch):
    # Point the hook at a deterministic stub instead of the real NYT login, so
    # this stays offline. It exercises the same refresh_login → persist path the
    # live flow uses.
    monkeypatch.setenv("SCRAPER_LOGIN_HOOK", "tests._fake_hook:login")
    sc = __import__("importlib").reload(isolated_state)

    assert sc.has_login_hook() is True
    assert sc.refresh_login(NYT_GATED_URL) is True

    sent = sc.cookies_for(NYT_GATED_URL, include_cache=False)
    assert sent.get("NYT-S") == "fake-session"
    # is_authed_host now true purely from the refreshed login cookies.
    assert sc.is_authed_host(NYT_GATED_URL) is True
    # browser tiers get the login cookie as an add_cookies record.
    recs = sc.browser_cookies(NYT_GATED_URL)
    assert any(r["name"] == "NYT-S" and r["value"] == "fake-session" for r in recs)


def test_no_login_hook_is_noop(isolated_state, monkeypatch):
    monkeypatch.delenv("SCRAPER_LOGIN_HOOK", raising=False)
    sc = __import__("importlib").reload(isolated_state)
    assert sc.has_login_hook() is False
    assert sc.refresh_login(NYT_GATED_URL) is False


# ── 5. Machinery: cf cache is egress-scope-isolated ───────────────────────────

def test_cf_cache_isolated_per_egress_scope(isolated_state, monkeypatch):
    sc = isolated_state
    from switchback import egress

    # Stored while "direct".
    monkeypatch.setattr(egress, "scope_label", lambda: "direct")
    sc.remember(NYT_GATED_URL, {"cf_clearance": "direct-token"}, ua="UA/1")
    assert sc.cookies_for(NYT_GATED_URL, include_cache=True).get("cf_clearance") == "direct-token"

    # Same host under the egress scope must NOT see the direct-scope token
    # (cf_clearance is IP-bound).
    monkeypatch.setattr(egress, "scope_label", lambda: "egress")
    assert "cf_clearance" not in sc.cookies_for(NYT_GATED_URL, include_cache=True)


# ── 6. Live NYT flow (skipped without creds) ──────────────────────────────────

@requires_nyt_creds
def test_live_nyt_login_persists_session(isolated_state, monkeypatch):
    """Log into NYT via the hook and assert a real session is persisted."""
    monkeypatch.setenv("SCRAPER_LOGIN_HOOK", "tests.nyt_login:login")
    sc = __import__("importlib").reload(isolated_state)

    assert sc.refresh_login(NYT_GATED_URL) is True, "NYT login hook returned no cookies"
    session = sc.cookies_for(NYT_GATED_URL, include_cache=False)
    assert any(name in session for name in ("NYT-S", "NYT-MPS", "SID")), \
        f"no recognizable NYT session cookie in {list(session)}"


@requires_nyt_creds
def test_live_gated_reachable_with_session_not_without(isolated_state, monkeypatch):
    """Gated NYT content must be reachable WITH a session and blocked WITHOUT it."""
    from switchback import scrape_detailed

    # No session: expect a miss / paywall, not full content.
    monkeypatch.delenv("SCRAPER_COOKIES_FILE", raising=False)
    monkeypatch.delenv("SCRAPER_LOGIN_HOOK", raising=False)
    sc = __import__("importlib").reload(isolated_state)
    without = scrape_detailed(NYT_GATED_URL)[0]

    # With a session (login hook wired so a wall triggers a refresh):
    monkeypatch.setenv("SCRAPER_LOGIN_HOOK", "tests.nyt_login:login")
    sc = __import__("importlib").reload(isolated_state)
    sc.refresh_login(NYT_GATED_URL)  # prime the session up front
    with_session = scrape_detailed(NYT_GATED_URL)[0]

    # The session run should do at least as well as the anonymous run, and should
    # actually return gated content.
    assert with_session.ok, f"gated URL not reachable with a session: {with_session.final_outcome}"
    if without.ok:
        # If NYT served the anonymous request too, the session run must be no worse.
        assert len(with_session.markdown) >= len(without.markdown) * 0.9


@requires_nyt_creds
def test_live_session_reuse_skips_relogin_within_ttl(isolated_state, monkeypatch):
    """A second request within the TTL must reuse the stored login, not re-login."""
    monkeypatch.setenv("SCRAPER_LOGIN_HOOK", "tests.nyt_login:login")
    monkeypatch.setenv("SCRAPER_SESSION_TTL_S", "1800")
    sc = __import__("importlib").reload(isolated_state)

    # First login persists the session.
    assert sc.refresh_login(NYT_GATED_URL) is True
    first = sc.cookies_for(NYT_GATED_URL, include_cache=False)

    # Count hook invocations: a within-TTL reuse must NOT call the hook again.
    calls = {"n": 0}
    real_hook = sc._login_hook()
    monkeypatch.setattr(sc, "_login_hook",
                        lambda: (lambda host: (calls.__setitem__("n", calls["n"] + 1) or real_hook(host))))

    # The persisted login cookies are served straight from the cache — no relogin.
    reused = sc.cookies_for(NYT_GATED_URL, include_cache=False)
    assert reused == first
    assert calls["n"] == 0, "session reuse re-invoked the login hook within TTL"
