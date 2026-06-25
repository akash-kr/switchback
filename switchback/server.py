"""HTTP service — the language-agnostic way to use the engine.

Wraps the same public functions as the library/CLI (`switchback.scrape`,
`switchback.search`) so any app, in any language, can hit one warm process (which
keeps the Tier-3 browser pool hot) instead of cold-starting a subprocess.

    pip install -e .[server]        # fastapi + uvicorn
    switchback-server           # or: python -m switchback.server
    curl localhost:8799/healthz
    curl -s localhost:8799/scrape -d '{"urls":["https://example.com"]}'
    curl 'localhost:8799/search?q=web+scraping'

Local-first by design: no auth, no rate-limiting. Put it behind your own gateway
if you expose it. Host/port via SCRAPER_HOST / SCRAPER_PORT.
"""
from __future__ import annotations

import os

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import session_trace
from .api import scrape
from .normalize import output_key
from .reporting import build_report, domain_report
from .search import search
from .tracing import setup_logs

app = FastAPI(title="switchback", version="0.2.0")


class ScrapeRequest(BaseModel):
    urls: list[str]
    format: str | None = None  # markdown (default) | markdown_trimmed | html | html_selectors


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/scrape")
def scrape_endpoint(req: ScrapeRequest) -> list[dict]:
    """Run URLs through the cascade. Returns successes only (failed URLs omitted).
    Optional "format" selects the output shape; the content key is "markdown" for
    markdown formats and "html" for html formats."""
    return [{"url": r.url, "source_method": r.source_method,
             output_key(r.format): r.markdown}
            for r in scrape(req.urls, fmt=req.format)]


@app.get("/search")
def search_endpoint(q: str) -> list[dict]:
    """Query → ranked URLs (SearXNG). Feed results back into /scrape."""
    return [{"title": h.title, "url": h.url, "snippet": h.snippet, "engine": h.engine}
            for h in search(q)]


def _since(minutes: int | None) -> datetime | None:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes) if minutes else None


@app.get("/metrics")
def metrics_endpoint(minutes: int | None = None) -> dict:
    """Metrics rollup from the engine's own state: cost savings vs Firecrawl,
    coverage, overall + per-tier latency, outcomes, and per-domain detail.
    Pass ?minutes=N to window the event-derived sections."""
    return build_report(since=_since(minutes))


@app.get("/metrics/domains")
def metrics_domains_endpoint(minutes: int | None = None) -> dict:
    """Per-domain table: error codes, challenges/bot-walls, and latency by host."""
    return domain_report(since=_since(minutes))


@app.get("/traces")
def list_traces_endpoint() -> list[dict]:
    """Captured Playwright session traces (opt-in via SCRAPER_TRACE_SESSION)."""
    return session_trace.list_traces()


@app.get("/traces/{trace_id}")
def get_trace_endpoint(trace_id: str):
    """Download one trace zip (open with `playwright show-trace <zip>`)."""
    path = session_trace.path_for(trace_id)
    if not path:
        raise HTTPException(status_code=404, detail="trace not found")
    return FileResponse(path, media_type="application/zip",
                        filename=f"{trace_id}.zip")


@app.delete("/traces/{trace_id}")
def delete_trace_endpoint(trace_id: str) -> dict:
    """Delete a trace zip."""
    if not session_trace.delete(trace_id):
        raise HTTPException(status_code=404, detail="trace not found")
    return {"deleted": trace_id}


def main() -> None:
    import logging
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    setup_logs()  # ship logs to the OTLP backend too when configured
    uvicorn.run(
        app,
        host=os.getenv("SCRAPER_HOST", "0.0.0.0"),
        port=int(os.getenv("SCRAPER_PORT", "8799")),
    )


if __name__ == "__main__":
    main()
