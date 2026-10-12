"""Unit tests for the opt-in extras: state JSON, appendix, query highlights (bm25 +
Jev), citation links, office docs, YouTube transcripts, and the MCP server — plus
the guarantee that a call without extras behaves exactly as before.

Pure + offline: fake tiers, botwall/content_cache stubbed, network mocked.
Run with: pytest tests/test_extras.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import types
from dataclasses import asdict, astuple, fields

import pytest

import switchback.orchestrator as orch
from switchback import api, highlights as H, normalize as N, state as S
from switchback.tiers import tier_1, tier_2

URL = "https://example.test/post"

PAGE = """<html><head><title>T</title>
<script type="application/ld+json">{"@type": "NewsArticle", "headline": "Solar costs fall"}</script>
<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"price": 42}}}</script>
<script>window.__INITIAL_STATE__ = {"user": null, "items": [1, 2]}; var x = 1;</script>
<script>window.__BROKEN__ = {a: undefined};</script>
<script>window.__LOADABLE_REQUIRED_CHUNKS__=[12,34];</script>
<script>self.__APOLLO_STATE__ = JSON.parse("{\\"k\\": \\"v\\"}");</script>
</head><body>
<nav><a href="/">Home</a> <a href="/world">World</a></nav>
<header>Site banner</header>
<article>
<h1>Solar power costs</h1>
<p>Solar panel prices fell by 30 percent in 2025 as factories in China expanded output
and polysilicon became cheaper. Installers passed most of the saving to households.</p>
<h2>Wind</h2>
<p>Offshore wind projects were delayed by higher interest rates and supply chain trouble,
and several developers cancelled contracts with state governments last year.</p>
<p>""" + ("Filler sentence about unrelated matters of local gardening. " * 40) + """</p>
</article>
<aside><a href="/related">Related: battery storage</a></aside>
<footer>(c) 2026 Example News</footer>
</body></html>"""


# ── harness ─────────────────────────────────────────────────────────────────

def _tier(name, fetch):
    return types.SimpleNamespace(NAME=name, PAID=False, fetch=fetch)


def _html_tier(url):
    return N.html_to_markdown(PAGE, base_url=url)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # The CLI entry point loads the repo's .env into os.environ; undo that after.
    saved_env = dict(os.environ)
    monkeypatch.setattr(orch.botwall, "load_db", lambda: {})
    monkeypatch.setattr(orch.botwall, "save_db", lambda db: None)
    monkeypatch.setattr(orch.botwall, "is_skipped", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "is_url_skipped", lambda u, db: False)
    monkeypatch.setattr(orch.botwall, "needs_egress", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "winning_tier", lambda h, db: None)
    monkeypatch.setattr(orch.botwall, "log_final", lambda *a, **k: None)
    monkeypatch.setattr(orch.botwall, "record", lambda *a, **k: None)
    monkeypatch.setattr(orch.content_cache, "get", lambda u, fmt: None)
    monkeypatch.setattr(orch.content_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(N, "OUTPUT_FORMAT", "markdown")
    monkeypatch.delenv("SCRAPER_HIGHLIGHTS_GRADER", raising=False)
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", _html_tier)])
    monkeypatch.setattr(orch, "INDEX", {"fake": 0})
    yield
    os.environ.clear()
    os.environ.update(saved_env)


# ── backward compatibility ──────────────────────────────────────────────────

# The result shapes as shipped before the extras existed. A default scrape must
# keep exactly these: users asdict()/astuple() results into their own payloads.
RESULT_FIELDS = ["url", "markdown", "source_method", "format"]
OUTCOME_FIELDS = ["url", "ok", "markdown", "source_method", "final_outcome", "error_class",
                  "status_code", "latency_ms", "egress", "wire_bytes", "format", "attempts"]


def test_result_classes_keep_their_fields():
    assert [f.name for f in fields(orch.ScrapeResult)] == RESULT_FIELDS
    assert [f.name for f in fields(orch.ScrapeOutcome)] == OUTCOME_FIELDS


def test_default_scrape_is_unchanged():
    """No extras: same markdown as a plain render, same types and payload shape."""
    [r] = api.scrape(URL)
    assert r.markdown == N.html_to_markdown(PAGE, base_url=URL)
    assert type(r) is orch.ScrapeResult and list(asdict(r)) == RESULT_FIELDS
    [o] = api.scrape_detailed(URL)
    assert o.ok and type(o) is orch.ScrapeOutcome and list(asdict(o)) == OUTCOME_FIELDS


def _short_tier(url):
    raise orch.ShortContent("too short")


def test_default_failure_is_unchanged(monkeypatch):
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", _short_tier)])
    [o] = api.scrape_detailed(URL)
    assert not o.ok and type(o) is orch.ScrapeOutcome
    assert api.scrape(URL) == []


def test_extras_return_subclasses(monkeypatch):
    """Opt-in results are subclasses: isinstance checks and old fields still hold."""
    [r] = api.scrape(URL, query="solar")
    assert type(r) is orch.ScrapeResultWithExtras and isinstance(r, orch.ScrapeResult)
    assert list(asdict(r)) == RESULT_FIELDS + ["state", "appendix", "highlights"]
    assert r.state is None and r.appendix is None and isinstance(r.highlights, list)
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", _short_tier)])
    [o] = api.scrape_detailed(URL, state=True)
    assert not o.ok and type(o) is orch.ScrapeOutcomeWithExtras and o.state is None


def test_default_path_does_not_capture(monkeypatch):
    """Without state/appendix the tier runs with no capture box active."""
    seen = []
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", lambda u: seen.append(N.current_capture())
                                              or _html_tier(u))])
    api.scrape(URL, query="solar")
    assert seen == [None]


def test_capture_does_not_change_markdown():
    plain = N.render(PAGE, URL)
    box: dict = {}
    with N.capture_into(box):
        captured = N.render(PAGE, URL)
    assert captured == plain and box["html"] == PAGE and box["base_url"] == URL


def test_positional_scrape_result_still_works():
    r = orch.ScrapeResult("u", "md", "tier_2")
    assert astuple(r) == ("u", "md", "tier_2", "markdown")


def test_cli_json_shape_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switchback", URL])
    monkeypatch.setattr("switchback.tracing.setup_logs", lambda: None)
    assert api._main() == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out[0]) == ["url", "source_method", "markdown"]


def test_cli_extras_flags(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["switchback", "--state", "--appendix",
                                      "--query", "solar panel prices", URL])
    monkeypatch.setattr("switchback.tracing.setup_logs", lambda: None)
    assert api._main() == 0
    [d] = json.loads(capsys.readouterr().out)
    assert list(d) == ["url", "source_method", "markdown", "state", "appendix", "highlights"]
    assert d["state"]["__NEXT_DATA__"]["props"]["pageProps"]["price"] == 42
    assert "Solar panel prices fell" in d["highlights"][0]["text"]


def test_server_shape(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from switchback.server import app
    c = TestClient(app)
    [d] = c.post("/scrape", json={"urls": [URL]}).json()
    assert list(d) == ["url", "source_method", "markdown"]
    [d] = c.post("/scrape", json={"urls": [URL], "state": True, "query": "wind"}).json()
    assert list(d) == ["url", "source_method", "markdown", "state", "highlights"]


# ── state JSON ──────────────────────────────────────────────────────────────

def test_extract_state():
    st = S.extract_state(PAGE)
    assert st["json_ld"] == [{"@type": "NewsArticle", "headline": "Solar costs fall"}]
    assert st["__NEXT_DATA__"]["props"]["pageProps"]["price"] == 42
    assert st["__INITIAL_STATE__"] == {"user": None, "items": [1, 2]}
    assert st["__APOLLO_STATE__"] == {"k": "v"}
    assert "__BROKEN__" not in st          # JS literal, not JSON: skipped, not guessed
    assert "__LOADABLE_REQUIRED_CHUNKS__" not in st   # bundler noise


def test_extract_state_empty_and_garbage():
    assert S.extract_state("") == {}
    assert S.extract_state("<script type='application/ld+json'>{nope</script>") == {}


def test_state_via_scrape():
    [r] = api.scrape(URL, state=True)
    assert r.state["json_ld"][0]["headline"] == "Solar costs fall"
    assert r.appendix is None


def test_state_empty_when_tier_has_no_html(monkeypatch):
    """API/PDF tiers never render HTML: requested extras come back empty, not None."""
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", lambda u: "TEXT " * 600)])
    [r] = api.scrape(URL, state=True, appendix=True)
    assert r.state == {} and r.appendix == []


def test_state_skips_content_cache(monkeypatch):
    monkeypatch.setattr(orch.content_cache, "get", lambda u, fmt: ("cached md", "tier_2"))
    [r] = api.scrape(URL)
    assert r.markdown == "cached md"                     # default still uses the cache
    [r] = api.scrape(URL, state=True)
    assert r.source_method == "fake" and r.state         # needs HTML → fetched


def test_state_from_worker_thread_tier(monkeypatch):
    """tier_3/tier_7 render on a worker thread; capture must follow them there."""
    import threading

    def threaded(url):
        cap, box = N.current_capture(), {}

        def work():
            with N.capture_into(cap):
                box["md"] = _html_tier(url)
        t = threading.Thread(target=work)
        t.start(); t.join()
        return box["md"]
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", threaded)])
    [r] = api.scrape(URL, state=True)
    assert r.state["__NEXT_DATA__"]


# ── appendix ────────────────────────────────────────────────────────────────

def test_appendix_regions():
    regions = N.appendix(PAGE, URL)
    assert [r["region"] for r in regions] == ["nav", "header", "aside", "footer"]
    assert "](https://example.test/world)" in regions[0]["markdown"]   # links resolved
    assert "Example News" in regions[-1]["markdown"]


def test_appendix_via_scrape_leaves_markdown_alone():
    [plain] = api.scrape(URL)
    [r] = api.scrape(URL, appendix=True)
    assert r.markdown == plain.markdown and len(r.appendix) == 4


# ── highlights ──────────────────────────────────────────────────────────────

def test_passages_keep_headings_with_text():
    md = N.html_to_markdown(PAGE, URL)
    ps = H.passages(md)
    assert any(p.startswith("## Wind") and "Offshore wind" in p for p in ps)
    assert all(len(p.split()) <= H._MAX_WORDS + 10 for p in ps)  # + a heading


def test_long_table_is_split_with_header_repeated():
    rows = "\n".join(f"| Canada | Widget {i} | {i} |" for i in range(200))
    md = "## Sheet1\n| Country | Product | Units |\n| --- | --- | --- |\n" + rows
    ps = H.passages(md)
    assert len(ps) > 3
    assert all("| Country | Product | Units |" in p for p in ps)
    assert all(len(p.split()) <= H._MAX_WORDS * 1.5 for p in ps)


def test_bm25_stems_terms():
    md = "| Country | Units |\n| --- | --- |\n| Canada | 3 |\n\nSomething else entirely here."
    hs = H.highlights(md, "which countries are listed", URL)
    assert hs and "Canada" in hs[0]["text"]


def test_bm25_ranks_relevant_passage_first():
    md = N.html_to_markdown(PAGE, URL)
    hs = H.highlights(md, "why were offshore wind projects delayed", URL)
    assert "Offshore wind" in hs[0]["text"] and hs[0]["grader"] == "bm25"
    assert hs[0]["link"].startswith(URL + "#:~:text=")


def test_no_match_returns_empty():
    assert H.highlights("Nothing relevant here at all.", "quantum chromodynamics", URL) == []
    assert H.highlights("", "x", URL) == []


def test_plain_text_unescapes_markdown():
    assert H.plain_text(r"Use snake\_case and 2\*3, *not* __bold__") == "Use snake_case and 2*3, not bold"


def test_plain_text_links():
    assert H.plain_text("tuned.[[3]](https://w.org/x#cite_note-3) More") == "tuned.[3] More"
    assert H.plain_text('the [compute](https://w.org/C_(x) "Compute (machine)") of') == "the compute of"
    assert H.plain_text("see <https://youtu.be/abc>") == "see https://youtu.be/abc"


def test_fragment_link_prefers_prose_over_tables():
    passage = "| Name | Last commit message and a very long cell |\n| --- | --- |\nShort prose line here."
    assert H.fragment_link("https://a.test/", passage).endswith("text=Short%20prose%20line%20here.")


def test_fragment_link():
    link = H.fragment_link("https://a.test/p", "## Head\n\nThe **quick** [brown](https://x) fox")
    assert link == "https://a.test/p#:~:text=The%20quick%20brown%20fox"
    long = "one-two three four five six seven eight nine ten eleven twelve, end"
    link = H.fragment_link("https://a.test/p#sec", long)
    assert link.startswith("https://a.test/p#sec:~:text=one%2Dtwo%20three")
    assert link.endswith(",nine%20ten%20eleven%20twelve%2C%20end")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_jev_grader(monkeypatch):
    monkeypatch.setenv("SCRAPER_HIGHLIGHTS_GRADER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        passages = json["state"]["passages"]
        return _Resp({"answers": {f"p{j}": {"type": "noul",
                                            "noul": 0.9 if "Offshore" in p else 0.1}
                                  for j, p in enumerate(passages)}})
    monkeypatch.setattr("requests.post", fake_post)
    md = N.html_to_markdown(PAGE, URL)
    hs = H.highlights(md, "what delayed wind farms?", URL)
    assert [h["grader"] for h in hs] == ["jev"]           # below 0.5 dropped
    assert "Offshore wind" in hs[0]["text"] and hs[0]["score"] == 0.9
    _, body, headers = calls[0]
    assert headers["Authorization"] == "Bearer test-key"
    assert body["questions"]["p0"]["type"] == "noul"
    assert "`passages[0]`" in body["questions"]["p0"]["instructions"]


def test_jev_failure_falls_back_to_bm25(monkeypatch):
    monkeypatch.setenv("SCRAPER_HIGHLIGHTS_GRADER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    def down(*a, **k):
        raise ConnectionError("api down")
    monkeypatch.setattr("requests.post", down)
    hs = H.highlights(N.html_to_markdown(PAGE, URL), "offshore wind delayed", URL)
    assert hs and hs[0]["grader"] == "bm25"


def test_jev_without_key_uses_bm25(monkeypatch):
    monkeypatch.setenv("SCRAPER_HIGHLIGHTS_GRADER", "jev")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert H.grader() == "bm25"


def test_jev_batches_respect_limits():
    chunks = ["x" * 30_000] * 5 + ["y"] * 100
    batches = H._jev_batches(chunks)
    assert sorted(i for b in batches for i in b) == list(range(105))
    assert all(len(b) <= H._JEV_BATCH_N for b in batches)
    assert all(sum(len(chunks[i]) for i in b) <= H._JEV_BATCH_CHARS for b in batches if len(b) > 1)


def test_highlights_error_never_fails_scrape(monkeypatch):
    monkeypatch.setattr(H, "highlights", lambda *a, **k: 1 / 0)
    [r] = api.scrape(URL, query="solar")
    assert r.highlights == [] and r.markdown


# ── office documents (tier_2) ───────────────────────────────────────────────

def test_doc_extension():
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert N.doc_extension(xlsx, "https://a.test/x") == ".xlsx"
    assert N.doc_extension("application/octet-stream", "https://a.test/r.docx?dl=1") == ".docx"
    assert N.doc_extension("text/html; charset=utf-8", "https://a.test/r.docx") is None
    assert N.doc_extension("application/pdf", "https://a.test/r.pdf") is None


def _docx_bytes():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Quarterly report", 1)
    for _ in range(60):
        d.add_paragraph("Revenue grew strongly across every region this quarter.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class _CffiResp:
    def __init__(self, content, ctype, url):
        self.status_code, self.content, self.url = 200, content, url
        self.headers = {"Content-Type": ctype}
        self.text = content.decode("latin-1")

    def close(self):
        pass


def _patch_cffi(monkeypatch, resp):
    fake = types.SimpleNamespace(requests=types.SimpleNamespace(get=lambda *a, **k: resp))
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake.requests)
    monkeypatch.setattr(tier_2.session_cache, "cookie_header", lambda *a, **k: None)


def test_tier2_docs_off_by_default(monkeypatch):
    """Without SCRAPER_DOCUMENTS the old path runs, even with markitdown installed."""
    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _patch_cffi(monkeypatch, _CffiResp(b"<p>" + b"legacy body text " * 200 + b"</p>",
                                       ctype, "https://a.test/r.docx"))
    monkeypatch.setattr(tier_2, "doc_bytes_to_markdown", lambda *a: pytest.fail("converted"))
    assert "legacy body text" in tier_2.fetch("https://a.test/r.docx")


def test_tier2_converts_docx(monkeypatch):
    pytest.importorskip("markitdown")
    monkeypatch.setenv("SCRAPER_DOCUMENTS", "1")
    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _patch_cffi(monkeypatch, _CffiResp(_docx_bytes(), ctype, "https://a.test/r.docx"))
    md = tier_2.fetch("https://a.test/r.docx")
    assert md.startswith("# Quarterly report") and "Revenue grew" in md


def test_tier2_docx_without_markitdown_takes_old_path(monkeypatch):
    """No markitdown → the pre-existing HTML path runs, as before this feature."""
    monkeypatch.setenv("SCRAPER_DOCUMENTS", "1")
    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _patch_cffi(monkeypatch, _CffiResp(b"<p>" + b"legacy body text " * 200 + b"</p>",
                                       ctype, "https://a.test/r.docx"))
    def no_markitdown(*a, **k):
        raise ImportError("no markitdown")
    monkeypatch.setattr(tier_2, "doc_bytes_to_markdown", no_markitdown)
    assert "legacy body text" in tier_2.fetch("https://a.test/r.docx")


# ── YouTube (tier_1) ────────────────────────────────────────────────────────

def test_youtube_url_matching():
    m = tier_1.YOUTUBE_RE.search
    assert m("https://www.youtube.com/watch?v=dQw4w9WgXcQ").group(1) == "dQw4w9WgXcQ"
    assert m("https://youtu.be/dQw4w9WgXcQ?t=3").group(1) == "dQw4w9WgXcQ"
    assert m("https://www.youtube.com/shorts/dQw4w9WgXcQ").group(1) == "dQw4w9WgXcQ"
    assert m("https://www.youtube.com/@channel") is None


def test_youtube_off_by_default(monkeypatch):
    """Without SCRAPER_YOUTUBE, YouTube URLs skip tier_1, even with the library."""
    _fake_yt(monkeypatch, [(0.0, "hello")])
    monkeypatch.delenv("SCRAPER_YOUTUBE")
    assert tier_1.fetch("https://youtu.be/dQw4w9WgXcQ") is None


def test_youtube_without_library_falls_through(monkeypatch):
    monkeypatch.setenv("SCRAPER_YOUTUBE", "1")
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", None)   # ImportError
    assert tier_1.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None


def _fake_yt(monkeypatch, snippets=None, error=None):
    class Api:
        def __init__(self, proxy_config=None, http_client=None):
            pass

        def fetch(self, vid, languages=("en",)):
            if error:
                raise error
            return [types.SimpleNamespace(start=s, text=t) for s, t in snippets]
    mod = types.ModuleType("youtube_transcript_api")
    mod.YouTubeTranscriptApi = Api
    prox = types.ModuleType("youtube_transcript_api.proxies")
    prox.GenericProxyConfig = lambda **k: k
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api.proxies", prox)
    monkeypatch.setattr(tier_1, "_youtube_meta", lambda url: ("A Talk", "A Channel"))
    monkeypatch.setenv("SCRAPER_YOUTUBE", "1")


def test_youtube_transcript(monkeypatch):
    _fake_yt(monkeypatch, [(0.0, "hello"), (12.5, "world"), (31.0, "second para"), (3700, "late")])
    md = tier_1.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert md.startswith("# A Talk\n\n**Channel:** A Channel")
    assert "[0:00](https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=0s) hello world" in md
    assert "[0:31](https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=31s) second para" in md
    assert "[1:01:40]" in md


def test_youtube_blocked_ip_retries_via_egress_proxy(monkeypatch):
    class RequestBlocked(Exception):
        pass
    seen = []

    def fake(vid, proxy_url):
        seen.append(proxy_url)
        if proxy_url is None:
            raise RequestBlocked("cloud IP")
        return [(0.0, "via proxy")]
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", types.ModuleType("y"))
    monkeypatch.setattr(tier_1, "_yt_snippets", fake)
    monkeypatch.setattr(tier_1, "_youtube_meta", lambda url: ("", ""))
    monkeypatch.setenv("SCRAPER_EGRESS_PROXY", "http://user:pass@proxy.test:80")
    monkeypatch.setenv("SCRAPER_YOUTUBE", "1")
    md = tier_1.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert seen == [None, "http://user:pass@proxy.test:80"] and "via proxy" in md
    monkeypatch.delenv("SCRAPER_EGRESS_PROXY")
    seen.clear()
    assert tier_1.fetch("https://youtu.be/dQw4w9WgXcQ") is None and seen == [None]


def test_youtube_tried_first_despite_learned_start(monkeypatch):
    """youtube.com learned tier_5: an opted-in video still tries tier_1 first, and
    without a transcript resumes at tier_5 (tier_2 stays skipped). Off: unchanged."""
    calls = []
    def t(name, md):
        return _tier(name, lambda u: calls.append(name) or md)
    monkeypatch.setattr(orch, "TIERS", [t("tier_1", None), t("tier_2", "x" * 3000),
                                        t("tier_5", "y" * 3000)])
    monkeypatch.setattr(orch, "INDEX", {"tier_1": 0, "tier_2": 1, "tier_5": 2})
    monkeypatch.setattr(orch.botwall, "winning_tier", lambda h, db: "tier_5")
    yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    api.scrape_detailed(yt)
    assert calls == ["tier_5"]
    monkeypatch.setenv("SCRAPER_YOUTUBE", "1")
    calls.clear()
    [o] = api.scrape_detailed(yt)
    assert calls == ["tier_1", "tier_5"] and o.source_method == "tier_5"
    calls.clear()
    api.scrape_detailed("https://example.test/page")
    assert calls == ["tier_5"]


def test_youtube_highlight_cites_timestamp():
    md = ("# T\n\n## Transcript\n\n[0:00](https://www.youtube.com/watch?v=abcdefghijk&t=0s) intro\n\n"
          "[12:05](https://www.youtube.com/watch?v=abcdefghijk&t=725s) attention is all you need")
    [h] = H.highlights(md, "attention", "https://youtu.be/abcdefghijk")
    assert h["link"] == "https://www.youtube.com/watch?v=abcdefghijk&t=725s"


def test_youtube_no_captions_falls_through(monkeypatch):
    _fake_yt(monkeypatch, error=RuntimeError("TranscriptsDisabled"))
    assert tier_1.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None


# ── MCP server ──────────────────────────────────────────────────────────────

def test_mcp_tools(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio
    from switchback import mcp_server as M
    tools = {t.name for t in asyncio.run(M.server.list_tools())}
    assert tools == {"scrape", "search"}
    ok = asyncio.run(M.scrape(URL, query="solar prices"))
    assert ok["ok"] and ok["markdown"] and ok["highlights"]
    monkeypatch.setattr(orch, "TIERS", [_tier("fake", lambda u: (_ for _ in ()).throw(
        orch.BotWall("cf", vendor="cloudflare")))])
    bad = asyncio.run(M.scrape(URL))
    assert bad == {"ok": False, "url": URL, "error": "botwall", "status_code": None}


def test_highlights_on_html_format_use_markdown_text():
    [r] = api.scrape(URL, fmt="html", query="offshore wind delayed")
    assert r.markdown.startswith("<html>")                 # content unchanged
    assert "Offshore wind" in r.highlights[0]["text"] and "<p>" not in r.highlights[0]["text"]
