"""Public entry point + CLI.

    from switchback import scrape
    results = scrape(["https://example.com/article"])

    # or:  python -m switchback.api <url> [<url> ...]
    #      python -m switchback.api --search <query ...>
"""
from __future__ import annotations

import sys

from .normalize import output_key
from .orchestrator import (Extras, ScrapeOutcome, ScrapeResult, TierAttempt, run,
                           run_detailed)
from .search import search  # re-export: query → URLs (SearXNG)


def _extras(state: bool, appendix: bool, query: str | None) -> Extras | None:
    """None when nothing extra was asked for, so the default path is untouched."""
    if state or appendix or (query and query.strip()):
        return Extras(state=bool(state), appendix=bool(appendix),
                      query=query.strip() if query and query.strip() else None)
    return None


def scrape(urls: str | list[str], fmt: str | None = None, *, state: bool = False,
           appendix: bool = False, query: str | None = None) -> list[ScrapeResult]:
    """Scrape one or many URLs through the cascade. Returns successes only.

    fmt selects the output format (markdown | markdown_trimmed | html |
    html_selectors); None uses the SCRAPER_OUTPUT_FORMAT default (markdown).
    For failures with classified reasons + the per-tier cascade, use
    scrape_detailed().

    Opt-in extras (each fills the matching result field; None when not asked):
      state=True     embedded page JSON (__NEXT_DATA__, JSON-LD, window.__X__ …)
      appendix=True  the nav/header/footer/aside stripped from the main content
      query="…"      highlights: the passages that answer it, with citation links"""
    if isinstance(urls, str):
        urls = [urls]
    return run(urls, fmt, _extras(state, appendix, query))


def scrape_detailed(urls: str | list[str], fmt: str | None = None, *,
                    state: bool = False, appendix: bool = False,
                    query: str | None = None) -> list[ScrapeOutcome]:
    """Like scrape() but returns a ScrapeOutcome per URL — successes *and*
    failures, each with final_outcome, error_class, status_code, and the
    per-tier attempts that were made. fmt and the extras as in scrape()."""
    if isinstance(urls, str):
        urls = [urls]
    return run_detailed(urls, fmt, _extras(state, appendix, query))


def result_dict(r: ScrapeResult, state: bool = False, appendix: bool = False,
                query: str | None = None) -> dict:
    """The JSON shape the CLI, HTTP service and MCP server return for a result.
    Extra keys appear only when that extra was requested."""
    d = {"url": r.url, "source_method": r.source_method, output_key(r.format): r.markdown}
    if state:
        d["state"] = r.state
    if appendix:
        d["appendix"] = r.appendix
    if query and query.strip():
        d["highlights"] = r.highlights
    return d


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
    usage = ("usage: switchback [--format FMT] [--state] [--appendix] [--query Q] <url> [<url> ...]\n"
             "       switchback --search <query ...>\n"
             "       switchback --doctor\n"
             "       (or: python -m switchback <url> ...)\n"
             "  FMT: markdown (default) | markdown_trimmed | html | html_selectors\n"
             "  --state     add the page's embedded JSON (Next.js, JSON-LD, ...)\n"
             "  --appendix  add the nav/header/footer/aside as separate markdown\n"
             "  --query Q   add highlights: passages that answer Q, with citation links")
    # --help/-h is an explicit request: usage to stdout, exit 0 (don't treat it
    # as a URL to scrape). Check before any work so it stays fast and side-effect-free.
    if any(a in ("--help", "-h") for a in sys.argv[1:]):
        print(usage)
        return 0
    # --doctor: preflight tier-readiness report (no scrape). Side-effect-free.
    if "--doctor" in sys.argv[1:]:
        from .doctor import report
        return report()
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
    # Optional --format / --format=FMT, --state, --appendix, --query / --query=Q
    # flags; everything else is a URL.
    fmt: str | None = None
    state = appendix = False
    query: str | None = None
    rest: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]; i += 2; continue
        if a.startswith("--format="):
            fmt = a.split("=", 1)[1]; i += 1; continue
        if a == "--state":
            state = True; i += 1; continue
        if a == "--appendix":
            appendix = True; i += 1; continue
        if a == "--query" and i + 1 < len(argv):
            query = argv[i + 1]; i += 2; continue
        if a.startswith("--query="):
            query = a.split("=", 1)[1]; i += 1; continue
        rest.append(a); i += 1
    if not rest:
        print(usage, file=sys.stderr)
        return 2
    results = scrape(rest, fmt=fmt, state=state, appendix=appendix, query=query)
    print(json.dumps([result_dict(r, state, appendix, query) for r in results],
                     indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(_main())
