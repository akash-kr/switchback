"""Shared content normalization — HTML→Markdown and PDF→text.

Ported from musings-by-hermes/scripts/muse_helpers.py (the most mature version):
strips boilerplate, promotes lazy-loaded images, resolves relative URLs.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def html_to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML → Markdown, preserving images/blockquotes/code.

    - Strips script/style/nav/header/footer/aside boilerplate.
    - Applies any per-domain extraction prefs (scope selector / extra drops),
      see switchback.extract.
    - Promotes lazy-load attrs (data-src, data-original, srcset) to src.
    - Resolves relative image/link URLs against base_url.
    """
    try:
        from markdownify import markdownify
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
            html = str(soup)
        except Exception as e:
            logger.debug(f"soup pre-clean skipped: {e}")
        md = markdownify(html, heading_style="ATX", code_language="",
                         bullets="-", strip=["script", "style"])
        return (md or "").strip()
    except Exception as e:
        logger.warning(f"markdownify failed: {e}")
        return (html or "").strip()


def pdf_bytes_to_text(data: bytes) -> str:
    """Extract text from PDF bytes. In-memory only — nothing written to disk."""
    from pypdf import PdfReader
    buf = io.BytesIO(data)
    try:
        reader = PdfReader(buf)
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    finally:
        buf.close()
