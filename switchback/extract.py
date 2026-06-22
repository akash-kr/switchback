"""Per-domain extraction preferences — remember how to carve each site.

Markdown of the whole page is the default. Some sites need scoping (drop the
mega-nav, keep the article) or specific elements pulled out. Rather than hardcode
that per tier, declare it once per host in ``config/extraction.json`` and every
tier's normalize step picks it up:

    {
      "www.example.com": {"selector": "article.main", "drop": [".ad", ".related"]},
      "blog.example.org": {"selector": "main .post-body"}
    }

  selector  CSS selector to scope to (first match wins); page minus the rest.
  drop      extra CSS selectors to remove before converting (ads, share bars).

Matching is by exact host (FQDN), consistent with the botwall policy. Absent /
unparseable config → no prefs, default whole-page markdown. Override the path
with SCRAPER_EXTRACTION_FILE.
"""
from __future__ import annotations

import json
import logging
import os

from .policy.gates import host_of

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_FILE = os.path.join(_PROJECT_ROOT, "config", "extraction.json")
PREFS_FILE = os.getenv("SCRAPER_EXTRACTION_FILE", _DEFAULT_FILE)

_PREFS: dict | None = None


def _load() -> dict:
    global _PREFS
    if _PREFS is not None:
        return _PREFS
    prefs: dict = {}
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE) as f:
                prefs = json.load(f) or {}
        except Exception as e:
            logger.warning(f"extract: could not read {PREFS_FILE}: {e}")
    _PREFS = prefs
    return _PREFS


def prefs_for(url: str | None) -> dict:
    """Extraction prefs for this URL's host, or {} when none are configured."""
    if not url:
        return {}
    return _load().get(host_of(url), {})
