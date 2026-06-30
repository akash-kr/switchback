"""Tier 0 — direct APIs / open mirrors.

Cheapest, cleanest, most reliable. Pattern-routed: returns None when no mirror
matches (caller falls through to Tier 1). Bypasses the botwall skip-list because
these are open, stable endpoints — not the scraped page.

Extend here with career-ops-style structured providers (greenhouse/lever/ashby).
Web *search* (query → URLs) is a different shape and lives in switchback/search.py
(local SearXNG), not in this fetch cascade.
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from ..normalize import html_to_markdown, UA
from ..policy.gates import check

NAME = "tier_1"
PAID = False

# Per-tier request timeout (seconds); override with SCRAPER_TIER_1_TIMEOUT_S.
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_1_TIMEOUT_S", "15"))

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", re.I)
WIKI_RE = re.compile(r"en\.wikipedia\.org/wiki/([^?#]+)", re.I)
PMC_RE = re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.I)


def fetch(url: str) -> str | None:
    m = ARXIV_RE.search(url)
    if m:
        return _arxiv(m.group(1), url)
    m = WIKI_RE.search(url)
    if m:
        return _wikipedia(m.group(1), url)
    if PMC_RE.search(url):
        return _europepmc(url)
    return None  # no mirror — fall through


def _arxiv(arxiv_id: str, url: str) -> str:
    # arxiv wants plain requests + an identifying UA (their published guidance);
    # impersonating Chrome triggers aggressive 429s from their Akamai front-end.
    import requests
    r = requests.get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}",
                     timeout=_TIMEOUT_S,
                     headers={"User-Agent": "switchback/1.0 (mailto:akash@theaklabs.com)"})
    r.raise_for_status()
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = ET.fromstring(r.text).find("atom:entry", ns)
    if entry is None:
        raise RuntimeError("arxiv: no entry in API response")
    title = (entry.findtext("atom:title", "", ns) or "").strip()
    summary = (entry.findtext("atom:summary", "", ns) or "").strip()
    authors = [a.findtext("atom:name", "", ns) or "" for a in entry.findall("atom:author", ns)]
    md = (f"# {title}\n\n**Authors:** {', '.join(a for a in authors if a)}\n\n"
          f"**arXiv:** {arxiv_id}\n\n## Abstract\n\n{summary}")
    return check(url, md)


def _wikipedia(title: str, url: str) -> str:
    from curl_cffi import requests as cffi
    r = cffi.get(f"https://en.wikipedia.org/api/rest_v1/page/html/{unquote(title)}",
                 timeout=_TIMEOUT_S, impersonate="chrome")
    r.raise_for_status()
    return check(url, html_to_markdown(r.text, base_url=url))


def _europepmc(url: str) -> str:
    # PMC full text via EuropePMC mirror (avoids reCAPTCHA on ncbi).
    import requests
    pmcid = PMC_RE.search(url).group(1)
    api = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    r = requests.get(api, timeout=_TIMEOUT_S, headers={"User-Agent": UA})
    r.raise_for_status()
    if len(r.text) < 1000:
        raise RuntimeError(f"europepmc empty: {len(r.text)}")
    return check(url, html_to_markdown(r.text, base_url=url))
