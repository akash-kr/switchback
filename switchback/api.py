"""Public entry point + CLI.

    from switchback import scrape
    results = scrape(["https://example.com/article"])

    # or:  python -m switchback.api <url> [<url> ...]
    #      python -m switchback.api --search <query ...>
"""
from __future__ import annotations

import sys

from .orchestrator import ScrapeOutcome, ScrapeResult, TierAttempt, run, run_detailed
from .search import search  # re-export: query → URLs (SearXNG)


def scrape(urls: str | list[str]) -> list[ScrapeResult]:
    """Scrape one or many URLs through the cascade. Returns successes only.

    For failures with classified reasons + the per-tier cascade, use
    scrape_detailed()."""
    if isinstance(urls, str):
        urls = [urls]
    return run(urls)


def scrape_detailed(urls: str | list[str]) -> list[ScrapeOutcome]:
    """Like scrape() but returns a ScrapeOutcome per URL — successes *and*
    failures, each with final_outcome, error_class, status_code, and the
    per-tier attempts that were made."""
    if isinstance(urls, str):
        urls = [urls]
    return run_detailed(urls)


def _main() -> int:
    import json
    import logging
    import pathlib
    from .tracing import setup_logs
    # Auto-load .env from the repo root so OTEL/SCRAPER vars are set even when
    # invoked as a subprocess (parent process needn't export them explicitly).
    _env = pathlib.Path(__file__).parent.parent / ".env"
    if _env.exists():
        import os as _os
        for _line in _env.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _k = _k.strip()
                if _k and _k not in _os.environ:
                    _os.environ[_k] = _v.strip()
    usage = ("usage: switchback <url> [<url> ...]\n"
             "       switchback --search <query ...>\n"
             "       (or: python -m switchback <url> ...)")
    # --help/-h is an explicit request: usage to stdout, exit 0 (don't treat it
    # as a URL to scrape). Check before any work so it stays fast and side-effect-free.
    if any(a in ("--help", "-h") for a in sys.argv[1:]):
        print(usage)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    setup_logs()  # also ship logs to the OTLP backend when configured
    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        return 2
    if sys.argv[1] == "--search":
        hits = search(" ".join(sys.argv[2:]))
        print(json.dumps(
            [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in hits],
            indent=2))
        return 0 if hits else 1
    results = scrape(sys.argv[1:])
    print(json.dumps(
        [{"url": r.url, "source_method": r.source_method, "markdown": r.markdown}
         for r in results],
        indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(_main())
