"""Opt-in hardening for deployments that scrape URLs other people submit:
SCRAPER_DISABLE_CLOUDSCRAPER (skip tier_3) and SCRAPER_BROWSER_BLOCK_PRIVATE
(browser tiers refuse non-public addresses).

The first half pins the *default* behaviour — nothing changes unless a variable is
set — then checks each switch. Pure + offline. The live browser checks at the end
launch Chromium and only run with SWITCHBACK_BROWSER_TESTS=1.
Run with: pytest tests/test_untrusted_urls.py
"""
from __future__ import annotations

import http.server
import os
import socket
import threading
import types

import pytest

import switchback.orchestrator as orch
from switchback import doctor
from switchback.tiers import _browser, tier_3


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SCRAPER_DISABLE_CLOUDSCRAPER", raising=False)
    monkeypatch.delenv("SCRAPER_BROWSER_BLOCK_PRIVATE", raising=False)


# --- Defaults: nothing changes unless opted in ----------------------------------

class FakeContext:
    def __init__(self):
        self.routes, self.socket_routes = [], []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.socket_routes.append((pattern, handler))


def test_by_default_cloudscraper_runs_and_browsers_are_untouched():
    assert tier_3.disabled() is False
    assert _browser.context_options() == {}
    context = FakeContext()
    assert _browser.block_private_networks(context) is False
    assert context.routes == [] and context.socket_routes == []


def test_by_default_doctor_reports_and_exits_exactly_as_before(monkeypatch):
    monkeypatch.setattr(tier_3, "available", lambda: (True, "cloudscraper 3.0.0"))
    monkeypatch.setattr(doctor.tier_4, "available", lambda: (True, "patchright + Chromium ready"))
    assert doctor.probe()[0] == ("tier_3 (cloudscraper)", True, "cloudscraper 3.0.0")
    assert doctor.report() == 0
    monkeypatch.setattr(tier_3, "available", lambda: (False, "cloudscraper not installed"))
    assert doctor.report() == 1  # a missing tier_3 is still a failure by default


# --- SCRAPER_DISABLE_CLOUDSCRAPER ------------------------------------------------

@pytest.fixture()
def isolated_cascade(monkeypatch):
    """Stub policy + cache so the cascade runs without touching real state."""
    monkeypatch.setattr(orch.botwall, "load_db", lambda: {})
    monkeypatch.setattr(orch.botwall, "save_db", lambda db: None)
    monkeypatch.setattr(orch.botwall, "is_skipped", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "is_url_skipped", lambda u, db: False)
    monkeypatch.setattr(orch.botwall, "needs_egress", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "winning_tier", lambda h, db: "tier_3")
    monkeypatch.setattr(orch.botwall, "log_final", lambda *a, **k: None)
    monkeypatch.setattr(orch.botwall, "record", lambda *a, **k: None)
    monkeypatch.setattr(orch.content_cache, "get", lambda u, fmt: None)
    monkeypatch.setattr(orch.content_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(orch, "TIERS", orch.TIERS)
    monkeypatch.setattr(orch, "INDEX", orch.INDEX)


def _cascade(calls):
    def tier(name, **extra):
        def fetch(url):
            calls.append(name)
            return "# Page\n\n" + "Readable article text. " * 40
        return types.SimpleNamespace(NAME=name, PAID=False, fetch=fetch, **extra)
    tiers = [tier("tier_3", disabled=tier_3.disabled), tier("tier_4")]
    orch.TIERS = tiers
    orch.INDEX = {t.NAME: i for i, t in enumerate(tiers)}
    return orch.run_detailed(["https://example.test/post"])[0]


def test_cloudscraper_still_runs_when_not_opted_out(isolated_cascade):
    calls: list = []
    out = _cascade(calls)
    assert out.ok and calls == ["tier_3"]


def test_opting_out_skips_cloudscraper_even_for_a_host_it_used_to_win(isolated_cascade, monkeypatch):
    monkeypatch.setenv("SCRAPER_DISABLE_CLOUDSCRAPER", "1")
    calls: list = []
    out = _cascade(calls)
    # The host's learned winner was tier_3; it is skipped, not stranded on.
    assert out.ok and calls == ["tier_4"]
    assert [(a.tier, a.outcome) for a in out.attempts][0] == ("tier_3", "disabled")


def test_doctor_shows_an_opted_out_cloudscraper_without_failing_the_healthcheck(monkeypatch):
    monkeypatch.setenv("SCRAPER_DISABLE_CLOUDSCRAPER", "1")
    monkeypatch.setattr(tier_3, "available", lambda: (True, "cloudscraper 3.0.0"))
    monkeypatch.setattr(doctor.tier_4, "available", lambda: (True, "patchright + Chromium ready"))
    assert doctor.probe()[0] == ("tier_3 (cloudscraper)", False, "off (SCRAPER_DISABLE_CLOUDSCRAPER set)")
    assert doctor.report() == 0


# --- SCRAPER_BROWSER_BLOCK_PRIVATE -----------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8384/", "http://localhost/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://10.0.0.5/admin", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://100.100.100.100/", "ws://127.0.0.1:9222/devtools",
    "http://0.0.0.0/",
])
def test_non_public_addresses_are_recognised(url):
    assert _browser.is_private_url(url) is True


@pytest.mark.parametrize("url", [
    "https://1.1.1.1/", "https://8.8.8.8/x", "data:text/html,hi", "blob:https://example.com/abc", "about:blank",
])
def test_public_addresses_and_non_network_urls_pass(url):
    assert _browser.is_private_url(url) is False


def test_a_hostname_is_judged_by_where_it_resolves(monkeypatch):
    answers = {"intranet.example": "10.1.2.3", "news.example": "93.184.216.34"}
    looked_up: list = []

    def resolve(host, port, **kwargs):
        looked_up.append(host)
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 0))]

    monkeypatch.setattr(_browser.socket, "getaddrinfo", resolve)
    cache: dict = {}
    assert _browser.is_private_url("https://intranet.example/x", cache) is True
    assert _browser.is_private_url("https://news.example/x", cache) is False
    assert _browser.is_private_url("https://nowhere.example/x", cache) is False  # the browser fails it itself
    assert _browser.is_private_url("https://intranet.example/other", cache) is True
    assert looked_up == ["intranet.example", "news.example", "nowhere.example"]  # one lookup per host


class FakeRoute:
    def __init__(self, url):
        self.request = types.SimpleNamespace(url=url)
        self.result = None

    def abort(self, reason):
        self.result = f"abort:{reason}"

    def continue_(self):
        self.result = "continue"


class FakeSocket:
    def __init__(self, url):
        self.url, self.result = url, None

    def close(self):
        self.result = "closed"

    def connect_to_server(self):
        self.result = "connected"


def test_opted_in_the_browser_refuses_non_public_requests_and_sockets(monkeypatch):
    monkeypatch.setenv("SCRAPER_BROWSER_BLOCK_PRIVATE", "1")
    assert _browser.context_options() == {"service_workers": "block"}
    context = FakeContext()
    assert _browser.block_private_networks(context) is True
    (_, guard_request), = context.routes
    (_, guard_socket), = context.socket_routes

    outcomes = {}
    for url in ("https://1.1.1.1/article", "http://127.0.0.1:8384/rest/config", "http://169.254.169.254/"):
        route = FakeRoute(url)
        guard_request(route)
        outcomes[url] = route.result
    assert outcomes == {"https://1.1.1.1/article": "continue",
                        "http://127.0.0.1:8384/rest/config": "abort:blockedbyclient",
                        "http://169.254.169.254/": "abort:blockedbyclient"}
    sockets = [FakeSocket("wss://1.1.1.1/live"), FakeSocket("ws://127.0.0.1:9222/devtools")]
    for ws in sockets:
        guard_socket(ws)
    assert [ws.result for ws in sockets] == ["connected", "closed"]


# --- Live: a real Chromium, a real local server -----------------------------------

live = pytest.mark.skipif(os.getenv("SWITCHBACK_BROWSER_TESTS") != "1",
                          reason="launches Chromium; set SWITCHBACK_BROWSER_TESTS=1")


def _serve(pages: dict, hits: list):
    """A local server with the given path → HTML pages, recording every path asked for."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = pages.get(self.path.split("?")[0], "secret local data").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            # Readable cross-origin, so only the guard (not CORS) can stop the fetch.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@live
@pytest.mark.parametrize("opted_in", [False, True])
def test_live_chromium_page_cannot_reach_a_local_service_when_opted_in(monkeypatch, opted_in):
    """The scraped page itself is allowed (the test treats only the 'internal' port as
    non-public); its fetch() of the internal service must be blocked when opted in,
    and must go through exactly as before when not."""
    from patchright.sync_api import sync_playwright

    internal_hits: list = []
    internal = _serve({}, internal_hits)
    page_hits: list = []
    port = internal.server_address[1]
    page_html = (f"<html><body><p id=out>waiting</p><script>"
                 f"fetch('http://127.0.0.1:{port}/rest/config').then(r => r.text())"
                 f".then(t => out.textContent = 'leaked: ' + t, () => out.textContent = 'blocked')"
                 f"</script></body></html>")
    outer = _serve({"/": page_html}, page_hits)
    if opted_in:
        monkeypatch.setenv("SCRAPER_BROWSER_BLOCK_PRIVATE", "1")
    real = _browser.is_private_url
    monkeypatch.setattr(_browser, "is_private_url",
                        lambda url, cache=None: f":{port}/" in url and real(url, cache))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(**_browser.context_options())
            _browser.block_private_networks(context)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{outer.server_address[1]}/")
            page.wait_for_function("document.getElementById('out').textContent !== 'waiting'", timeout=10000)
            text = page.inner_text("#out")
            browser.close()
    finally:
        internal.shutdown()
        outer.shutdown()
    if opted_in:
        assert text == "blocked" and internal_hits == []
    else:
        assert text == "leaked: secret local data" and internal_hits == ["/rest/config"]


@live
@pytest.mark.parametrize("opted_in", [False, True])
def test_live_tier_4_scrape_keeps_local_data_out_of_the_result(monkeypatch, opted_in):
    """The same, end to end through tier_4.fetch: what the scrape returns."""
    from switchback.tiers import tier_4

    internal_hits: list = []
    internal = _serve({}, internal_hits)
    port = internal.server_address[1]
    article = "".join(f"<p>Paragraph {n} of an ordinary article about gardening: when to water, "
                      f"which soil suits tomatoes, and how to keep slugs off the lettuce bed.</p>" for n in range(40))
    page_html = (f"<html><head><title>Garden</title></head><body><h1>Garden</h1>{article}<p id=out>waiting</p><script>"
                 f"fetch('http://127.0.0.1:{port}/rest/config').then(r => r.text())"
                 f".then(t => out.textContent = 'leaked: ' + t, () => out.textContent = 'nothing loaded')"
                 f"</script></body></html>")
    outer = _serve({"/": page_html}, [])
    if opted_in:
        monkeypatch.setenv("SCRAPER_BROWSER_BLOCK_PRIVATE", "1")
    real = _browser.is_private_url
    monkeypatch.setattr(_browser, "is_private_url",
                        lambda url, cache=None: f":{port}/" in url and real(url, cache))
    try:
        markdown = tier_4.fetch(f"http://127.0.0.1:{outer.server_address[1]}/")
    finally:
        internal.shutdown()
        outer.shutdown()
    assert "gardening" in markdown
    if opted_in:
        assert "secret local data" not in markdown and internal_hits == []
    else:
        assert "leaked: secret local data" in markdown and internal_hits == ["/rest/config"]
