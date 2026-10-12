"""MCP server — the scrape cascade and search as tools for AI agents.

    pip install "switchback[mcp]"
    switchback-mcp                                  # stdio transport
    claude mcp add switchback -- switchback-mcp     # e.g. register with Claude Code

Tools:
  scrape(url, format, query, state, appendix)  one URL through the cascade
  search(query)                                 query → URLs (local SearXNG)

Scrapes run one at a time on a worker thread: the browser tiers use Playwright's
sync API, which refuses to run on a thread with an asyncio loop, and serialising
keeps the per-host policy DB writes from racing (same as the CLI).
"""
from __future__ import annotations

import asyncio

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from .api import result_dict, scrape_detailed
from .search import search as _search

server = MCPServer(
    "switchback",
    instructions=("Fetch web pages as clean markdown. Use `scrape` for a known URL "
                  "(pass `query` to get only the passages that answer a question, "
                  "with citation links), and `search` to find URLs first."),
)
_lock = asyncio.Lock()


@server.tool()
async def scrape(url: str, format: str = "markdown", query: str | None = None,
                 state: bool = False, appendix: bool = False) -> dict:
    """Fetch one URL as clean content, cheapest method first (HTTP, then stealth
    browsers, then a paid API if configured).

    Args:
        url: the page, PDF, office document or YouTube video to fetch.
        format: markdown (default) | markdown_trimmed | html | html_selectors.
        query: optional question; adds `highlights`, the passages that answer it,
            each with a score and a link that jumps to that text on the page.
        state: add `state`, the JSON the page embeds for its own frontend
            (Next.js data, JSON-LD, window globals) — often cleaner than the text.
        appendix: add `appendix`, the page's menus/header/footer/sidebars as
            separate markdown (they are stripped from the main content).

    Returns {"ok": true, "url", "source_method", "markdown"|"html", ...extras}
    or {"ok": false, "url", "error", "status_code"} when every method failed."""
    async with _lock:
        outcome = (await asyncio.to_thread(
            scrape_detailed, [url], format, state=state, appendix=appendix, query=query))[0]
    if not outcome.ok:
        return {"ok": False, "url": url,
                "error": outcome.error_class or outcome.final_outcome,
                "status_code": outcome.status_code}
    return {"ok": True, **result_dict(outcome, state, appendix, query)}


@server.tool()
async def search(query: str) -> list[dict]:
    """Web search through the local SearXNG instance (SEARXNG_URL). Returns
    [{"title", "url", "snippet"}]; feed a URL into `scrape` to read it."""
    hits = await asyncio.to_thread(_search, query)
    return [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in hits]


def main() -> None:
    import logging
    # stdout is the MCP channel: logs must go to stderr (logging's default).
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    server.run()


if __name__ == "__main__":
    main()
