"""Shared content normalization — HTML→Markdown, PDF→text, office docs→Markdown.

Ported from musings-by-hermes/scripts/muse_helpers.py (the most mature version):
strips boilerplate, promotes lazy-loaded images, resolves relative URLs.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── Output format ───────────────────────────────────────────────────────────
# Default is markdown (today's behavior, byte-identical). Opt into other shapes
# globally via SCRAPER_OUTPUT_FORMAT, or per-call via the scrape(fmt=...) /
# --format / {"format": ...} overrides which set output_format_scope().
#   markdown          whole-page markdown (default)
#   markdown_trimmed  markdown with extra ad/nav/boilerplate lines removed
#   html              raw HTML exactly as fetched (no cleaning)
#   html_selectors    cleaned HTML (boilerplate strip + per-domain drop/selector),
#                     not converted to markdown
VALID_FORMATS = ("markdown", "markdown_trimmed", "html", "html_selectors")


def _validate(fmt: str | None) -> str:
    f = (fmt or "").strip().lower()
    if f not in VALID_FORMATS:
        logger.warning(f"unknown output format {fmt!r}; using 'markdown' "
                       f"(valid: {', '.join(VALID_FORMATS)})")
        return "markdown"
    return f


OUTPUT_FORMAT = _validate(os.getenv("SCRAPER_OUTPUT_FORMAT", "markdown"))

# Per-thread active-format override. Thread-local by construction (like egress):
# the orchestrator sets it on the worker thread that also runs the tier fetch +
# this module's conversion, so concurrent server requests can't bleed formats.
_scope = threading.local()


@contextmanager
def output_format_scope(fmt: str | None):
    """Set the active output format for the enclosed work (per-thread). A falsy
    fmt means 'use the SCRAPER_OUTPUT_FORMAT default'. Always restored on exit."""
    prev = getattr(_scope, "fmt", None)
    _scope.fmt = _validate(fmt) if fmt else None
    try:
        yield
    finally:
        _scope.fmt = prev


def active_format() -> str:
    """The format in effect for the current thread: the per-call override if set,
    else the SCRAPER_OUTPUT_FORMAT default."""
    return getattr(_scope, "fmt", None) or OUTPUT_FORMAT


# Opt-in raw-HTML capture. The extras that need the page itself (state JSON, the
# appendix) set a box here around a tier's fetch; render() drops the HTML it was
# given into the box, so no tier has to change what it returns. The last render
# wins, which is the one that produced the tier's content (a browser tier's
# interstitial check renders first, then the real page). Thread-local like the
# format scope; tiers that render on a worker thread re-apply it with capture_into.
_capture = threading.local()


@contextmanager
def capture_into(box: dict | None):
    """Record the HTML render() sees into `box` for the enclosed work (per-thread).
    None disables capture. Always restored on exit."""
    prev = getattr(_capture, "box", None)
    _capture.box = box
    try:
        yield box
    finally:
        _capture.box = prev


def current_capture() -> dict | None:
    """The active capture box on this thread, for a tier to hand to its worker."""
    return getattr(_capture, "box", None)


def output_key(fmt: str) -> str:
    """The JSON/result key for a format's content family: html-family → "html",
    markdown-family → "markdown". Lets the default path stay {"...","markdown"}."""
    return "html" if fmt.startswith("html") else "markdown"


def _clean_html(html: str, base_url: str | None = None) -> str:
    """Return cleaned HTML: boilerplate stripped, per-domain drop/selector applied
    (see switchback.extract), lazy-load image attrs promoted, relative image/link
    URLs resolved against base_url. On any failure returns `html` unchanged."""
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin

        from .extract import prefs_for
        prefs = prefs_for(base_url)

        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header",
                         "footer", "aside", "form", "iframe"]):
            tag.decompose()
        # Per-domain: remove configured noise, then scope to the content node.
        for sel in prefs.get("drop", []):
            for tag in soup.select(sel):
                tag.decompose()
        selector = prefs.get("selector")
        if selector:
            node = soup.select_one(selector)
            if node is not None:
                soup = BeautifulSoup(str(node), "html.parser")
            else:
                logger.debug(f"extract: selector {selector!r} matched nothing for {base_url}")
        for img in soup.find_all("img"):
            src = (img.get("src") or img.get("data-src")
                   or img.get("data-original") or img.get("data-lazy-src"))
            if not src and img.get("srcset"):
                src = img["srcset"].split(",")[0].strip().split(" ")[0]
            if src:
                if base_url:
                    src = urljoin(base_url, src)
                img["src"] = src
        if base_url:
            for a in soup.find_all("a", href=True):
                a["href"] = urljoin(base_url, a["href"])
        return str(soup)
    except Exception as e:
        logger.debug(f"soup pre-clean skipped: {e}")
        return html


# Lines that markdown_trimmed drops: standalone images, link-only/nav rows, and
# short promotional boilerplate. Conservative on purpose — prose is never touched.
_TRIM_IMG_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
_TRIM_LINKS_ONLY_RE = re.compile(r"^(?:[-*>]\s*)?(?:\[[^\]]*\]\([^)]*\)[\s|·•\-–—]*)+$")
_TRIM_BOILERPLATE_RE = re.compile(
    r"^(subscribe|sign\s*up|sign\s*in|log\s*in|logout|newsletter|advertisement|"
    r"accept\s+all|cookie|follow\s+us|share\s+this)\b", re.I)


def _trim_markdown(md: str) -> str:
    """Markdown minus common ad/nav/boilerplate noise. Drops only standalone-image
    lines, link-only/nav rows, and short promotional boilerplate lines; keeps all
    prose. Collapses 3+ blank lines to one."""
    kept: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            kept.append("")
            continue
        if _TRIM_IMG_RE.match(s) or _TRIM_LINKS_ONLY_RE.match(s):
            continue
        if len(s) <= 60 and _TRIM_BOILERPLATE_RE.match(s):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def render(html: str, base_url: str | None = None, fmt: str | None = None) -> str:
    """Render fetched HTML in `fmt` (default: the active output format).

    - markdown          whole-page markdown (boilerplate strip + per-domain prefs)
    - markdown_trimmed  markdown with extra ad/nav/boilerplate lines removed
    - html              the raw HTML, untouched
    - html_selectors    cleaned HTML (per-domain prefs applied), not converted
    """
    box = getattr(_capture, "box", None)
    if box is not None:
        box["html"], box["base_url"] = html, base_url
    fmt = _validate(fmt) if fmt is not None else active_format()
    if fmt == "html":
        return html or ""
    if fmt == "html_selectors":
        return (_clean_html(html, base_url) or "").strip()
    try:
        from markdownify import markdownify
        cleaned = _clean_html(html, base_url)
        md = (markdownify(cleaned, heading_style="ATX", code_language="",
                          bullets="-", strip=["script", "style"]) or "").strip()
        return _trim_markdown(md) if fmt == "markdown_trimmed" else md
    except Exception as e:
        logger.warning(f"markdownify failed: {e}")
        return (html or "").strip()


def html_to_markdown(html: str, base_url: str | None = None) -> str:
    """Render `html` in the active output format (default markdown). Name kept for
    back-compat: every tier calls this, so it automatically honors the selected
    SCRAPER_OUTPUT_FORMAT / per-call format with no per-tier changes."""
    return render(html, base_url)


# Page chrome that _clean_html strips from the main content. The appendix keeps it
# instead, one entry per outermost region, so an agent can ask for the menus,
# footer or "related" links on demand without them padding the main markdown.
_APPENDIX_TAGS = ["nav", "header", "footer", "aside"]


def appendix(html: str, base_url: str | None = None) -> list[dict]:
    """The secondary regions of a page as [{"region": tag, "markdown": md}], in
    page order. Nested regions stay inside their outermost parent. Empty regions
    are left out; on any failure returns []."""
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
        from urllib.parse import urljoin

        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        regions = []
        for tag in soup.find_all(_APPENDIX_TAGS):
            if tag.find_parent(_APPENDIX_TAGS):
                continue
            if base_url:
                for a in tag.find_all("a", href=True):
                    a["href"] = urljoin(base_url, a["href"])
            md = (markdownify(str(tag), heading_style="ATX", bullets="-") or "").strip()
            if md:
                regions.append({"region": tag.name, "markdown": re.sub(r"\n{3,}", "\n\n", md)})
        return regions
    except Exception as e:
        logger.warning(f"appendix extraction failed: {e}")
        return []


def pdf_bytes_to_text(data: bytes) -> str:
    """Extract text from PDF bytes. In-memory only — nothing written to disk."""
    from pypdf import PdfReader
    buf = io.BytesIO(data)
    try:
        reader = PdfReader(buf)
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    finally:
        buf.close()


# Office / ebook files converted by markitdown (optional: `pip install
# "switchback[documents]"`). Matched by Content-Type, else by the URL's extension.
# PDFs are not listed: they keep their own pypdf path above.
DOC_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/epub+zip": ".epub",
}


def doc_extension(content_type: str, url: str) -> str | None:
    """The office/ebook extension this response is, or None when it isn't one."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = DOC_TYPES.get(ctype)
    if ext or "html" in ctype:  # a page served as HTML is HTML, whatever its path
        return ext
    path = (url or "").lower().split("?")[0].split("#")[0]
    return next((e for e in DOC_TYPES.values() if path.endswith(e)), None)


def doc_bytes_to_markdown(data: bytes, extension: str) -> str:
    """Convert an office/ebook file to markdown with markitdown. In-memory only.
    Raises ImportError when markitdown isn't installed."""
    from markitdown import MarkItDown, StreamInfo
    buf = io.BytesIO(data)
    try:
        res = MarkItDown().convert_stream(buf, stream_info=StreamInfo(extension=extension))
        return (res.text_content or "").strip()
    finally:
        buf.close()
