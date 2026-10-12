"""Tier 0 — direct APIs / open mirrors.

Cheapest, cleanest, most reliable. Pattern-routed: returns None when no mirror
matches (caller falls through to Tier 1). Bypasses the botwall skip-list because
these are open, stable endpoints — not the scraped page.

YouTube videos return their transcript when SCRAPER_YOUTUBE=1 and
youtube-transcript-api is installed (`switchback[youtube]`); otherwise, or when a
video has no captions, the URL falls through to the HTTP tiers exactly as before.

Extend here with career-ops-style structured providers (greenhouse/lever/ashby).
Web *search* (query → URLs) is a different shape and lives in switchback/search.py
(local SearXNG), not in this fetch cascade.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from ..egress import has_egress_proxy, requests_proxies
from ..normalize import html_to_markdown, UA
from ..policy.gates import check

logger = logging.getLogger(__name__)

NAME = "tier_1"
PAID = False

# Per-tier request timeout (seconds); override with SCRAPER_TIER_1_TIMEOUT_S.
_TIMEOUT_S = float(os.getenv("SCRAPER_TIER_1_TIMEOUT_S", "15"))

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", re.I)
WIKI_RE = re.compile(r"en\.wikipedia\.org/wiki/([^?#]+)", re.I)
PMC_RE = re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.I)
YOUTUBE_RE = re.compile(
    r"(?:(?:www\.|m\.)?youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)"
    r"|youtu\.be/)([A-Za-z0-9_-]{11})", re.I)

# Transcript languages to try, in order (comma-separated codes).
_YT_LANGS = [c.strip() for c in os.getenv("SCRAPER_YOUTUBE_LANGS", "en").split(",") if c.strip()]
# Group transcript lines into one timestamped paragraph per this many seconds.
_YT_PARAGRAPH_S = 30


def fetch(url: str) -> str | None:
    m = ARXIV_RE.search(url)
    if m:
        return _arxiv(m.group(1), url)
    m = WIKI_RE.search(url)
    if m:
        return _wikipedia(m.group(1), url)
    if PMC_RE.search(url):
        return _europepmc(url)
    m = YOUTUBE_RE.search(url)
    if m and os.getenv("SCRAPER_YOUTUBE") in ("1", "true", "True"):  # opt-in: changes output
        return _youtube(m.group(1), url)
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


def _youtube(video_id: str, url: str) -> str | None:
    """Title + timestamped transcript for a YouTube video. Returns None (not
    applicable — the cascade carries on as it did before) when the library is
    missing or the video has no usable transcript, rather than recording a miss
    against youtube.com."""
    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        return None
    proxies = requests_proxies()
    try:
        snippets = _yt_snippets(video_id, proxies["https"] if proxies else None)
    except Exception as e:  # no captions, wrong language, blocked IP, network
        # YouTube blocks most cloud/datacenter IPs. Like the rest of the cascade,
        # retry through the residential egress proxy only for this blocked case.
        blocked = type(e).__name__ in ("RequestBlocked", "IpBlocked")
        if blocked and not proxies and has_egress_proxy():
            logger.info(f"{NAME} youtube blocked our IP; retrying {video_id} via egress proxy")
            try:
                snippets = _yt_snippets(video_id, os.environ["SCRAPER_EGRESS_PROXY"])
            except Exception as e2:
                logger.info(f"{NAME} youtube transcript unavailable for {video_id}: "
                            f"{type(e2).__name__}")
                return None
        else:
            logger.info(f"{NAME} youtube transcript unavailable for {video_id}: "
                        f"{type(e).__name__}" + (" (set SCRAPER_EGRESS_PROXY)" if blocked else ""))
            return None
    snippets = [(t, text) for t, text in snippets if text]
    if not snippets:
        return None

    title, channel = _youtube_meta(url)
    paras: list[str] = []
    start, buf = snippets[0][0], []
    for t, text in snippets:
        if buf and t - start >= _YT_PARAGRAPH_S:
            paras.append(_yt_para(video_id, start, buf))
            start, buf = t, []
        buf.append(text)
    paras.append(_yt_para(video_id, start, buf))
    head = f"# {title}\n\n" if title else ""
    if channel:
        head += f"**Channel:** {channel}\n\n"
    return f"{head}**Video:** https://www.youtube.com/watch?v={video_id}\n\n## Transcript\n\n" + "\n\n".join(paras)


def _yt_snippets(video_id: str, proxy_url: str | None) -> list[tuple[float, str]]:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig
    proxy = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
    api = YouTubeTranscriptApi(proxy_config=proxy, http_client=_TimeoutSession())
    return [(float(s.start), (s.text or "").strip())
            for s in api.fetch(video_id, languages=_YT_LANGS)]


def _TimeoutSession():
    """A requests.Session whose calls default to the tier timeout — the transcript
    library passes none, and a hung socket here would stall the whole cascade."""
    import requests

    class _S(requests.Session):
        def request(self, *a, **kw):
            kw.setdefault("timeout", _TIMEOUT_S)
            return super().request(*a, **kw)
    return _S()


def _yt_para(video_id: str, start: float, texts: list[str]) -> str:
    s = int(start)
    stamp = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    body = " ".join(texts).replace("\n", " ")
    return f"[{stamp}](https://www.youtube.com/watch?v={video_id}&t={s}s) {body}"


def _youtube_meta(url: str) -> tuple[str, str]:
    """(title, channel) from YouTube's public oEmbed endpoint; blanks on failure."""
    import requests
    try:
        r = requests.get("https://www.youtube.com/oembed", timeout=_TIMEOUT_S,
                         params={"url": url, "format": "json"}, proxies=requests_proxies())
        r.raise_for_status()
        d = r.json()
        return (d.get("title") or "").strip(), (d.get("author_name") or "").strip()
    except Exception:
        return "", ""
