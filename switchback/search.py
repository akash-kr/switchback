"""Web search via a local SearXNG instance (query → ranked URLs).

Distinct from the scrape cascade: scrape fetches one *known* URL; search
*discovers* URLs from a query. Returns lightweight results a caller can then
feed into scrape(). Points at the SearXNG container on localhost:8888 (override
with SEARXNG_URL); requires its JSON format to be enabled.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .tracing import span

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str


def search(query: str, n: int = 10) -> list[SearchResult]:
    """Query the local SearXNG and return up to n results."""
    import requests
    with span("search", **{"search.query": query, "search.n": n}) as sp:
        r = requests.get(f"{SEARXNG_URL}/search",
                         params={"q": query, "format": "json"}, timeout=15)
        r.raise_for_status()
        raw = r.json().get("results", [])[:n]
        results = [SearchResult(title=x.get("title", ""), url=x.get("url", ""),
                                snippet=x.get("content", ""),
                                engine=x.get("engine", ""))
                   for x in raw]
        sp.set("search.n_results", len(results))
        return results
