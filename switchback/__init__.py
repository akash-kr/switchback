"""switchback — one cost-ordered scrape cascade, used by every tool.

Public API:
    from switchback import scrape, search
    results = scrape(["https://example.com/article"])
    hits = search("web scraping")            # query → URLs (SearXNG)
"""
from .api import scrape, scrape_detailed, ScrapeOutcome, ScrapeResult, TierAttempt
from .search import search, SearchResult

__all__ = ["scrape", "scrape_detailed", "ScrapeOutcome", "ScrapeResult",
           "TierAttempt", "search", "SearchResult"]
