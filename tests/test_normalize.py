"""Unit tests for the output-format renderer (switchback.normalize).

Pure + offline: a fixed HTML fixture with real content plus every junk type, run
through all four formats. Run with: pytest tests/test_normalize.py
"""
from __future__ import annotations

import pytest

import switchback.normalize as N
from switchback import extract
from switchback.policy.gates import host_of

BASE = "https://test.example/post"

FIXTURE = """<html><head><title>T</title><style>.x{color:red}</style></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<header>Site banner</header>
<article class="post">
  <h1>The Real Title</h1>
  <p>First real paragraph of the article body, long enough to keep around.</p>
  <div class="ad">Advertisement buy now</div>
  <p>Second paragraph continues the actual content here for the reader.</p>
  <p><a href="/next">Read more</a></p>
  <p><img src="/banner.png"></p>
  <p>Subscribe to our newsletter</p>
</article>
<aside class="related">Related stories</aside>
<footer>(c) 2026 Example</footer>
<script>tracking()</script>
</body></html>"""


@pytest.fixture(autouse=True)
def _prefs(monkeypatch):
    """Per-domain prefs for the fixture host: scope to the article, drop the ad.
    Also pin the default format to markdown so env can't perturb assertions."""
    monkeypatch.setattr(
        extract, "_PREFS",
        {host_of(BASE): {"selector": "article.post", "drop": [".ad"]}})
    monkeypatch.setattr(N, "OUTPUT_FORMAT", "markdown")
    yield


def test_html_is_raw_and_untouched():
    r = N.render(FIXTURE, BASE, "html")
    assert "<nav>" in r
    assert "<script>" in r
    assert 'class="ad"' in r
    assert "<footer>" in r


def test_html_selectors_is_cleaned_subtree():
    r = N.render(FIXTURE, BASE, "html_selectors")
    # boilerplate + dropped node + out-of-scope content all gone
    assert "<nav>" not in r
    assert "<script>" not in r
    assert "<footer>" not in r
    assert "Advertisement" not in r
    assert "Related stories" not in r
    # the article subtree survives
    assert "<h1>" in r
    assert "The Real Title" in r
    assert "Second paragraph" in r


def test_markdown_is_unchanged_and_backcompat():
    r = N.render(FIXTURE, BASE, "markdown")
    assert "# The Real Title" in r
    assert "First real paragraph" in r
    assert "Second paragraph" in r
    assert "Advertisement" not in r          # dropped by per-domain `.ad`
    # html_to_markdown() must stay byte-identical to the default render
    assert N.html_to_markdown(FIXTURE, BASE) == r


def test_markdown_trimmed_drops_junk_keeps_prose():
    md = N.render(FIXTURE, BASE, "markdown")
    trimmed = N.render(FIXTURE, BASE, "markdown_trimmed")
    assert "Subscribe to our newsletter" not in trimmed
    assert "Read more" not in trimmed        # link-only line dropped
    assert "banner.png" not in trimmed       # standalone image dropped
    assert "First real paragraph" in trimmed
    assert "Second paragraph" in trimmed
    assert len(trimmed) < len(md)


def test_unknown_format_falls_back_to_markdown():
    assert N.render(FIXTURE, BASE, "bogus") == N.render(FIXTURE, BASE, "markdown")


def test_output_format_scope_overrides_default():
    assert N.active_format() == "markdown"
    with N.output_format_scope("html"):
        assert N.active_format() == "html"
    assert N.active_format() == "markdown"
    # a falsy override means "use the default"
    with N.output_format_scope(None):
        assert N.active_format() == "markdown"


def test_output_key_maps_family():
    assert N.output_key("markdown") == "markdown"
    assert N.output_key("markdown_trimmed") == "markdown"
    assert N.output_key("html") == "html"
    assert N.output_key("html_selectors") == "html"
