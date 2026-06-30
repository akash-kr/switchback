#!/usr/bin/env python3
"""
Scraper Engine Evaluation Suite
────────────────────────────────
Tests accuracy, latency, tier selection, and markdown quality across the
anti-bot difficulty spectrum — from open APIs to DataDome-protected job boards.

Usage:
    python test_suite.py                 # full suite (~5–10 min with browser tiers)
    python test_suite.py --quick         # tier0 + tier1 only  (~30s)
    python test_suite.py --filter tier2  # run one category
    python test_suite.py -v              # show markdown preview + cascade path
    python test_suite.py --deadline 60   # override per-URL timeout
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── ANSI colors (disabled when not a tty) ──────────────────────────────────────

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str) -> str:    return _c("31", t)
def cyan(t: str) -> str:   return _c("36", t)
def bold(t: str) -> str:   return _c("1",  t)
def dim(t: str) -> str:    return _c("2",  t)

# ── Test case definition ────────────────────────────────────────────────────────

@dataclass
class TestCase:
    url: str
    label: str
    # Category drives --quick / --filter filtering:
    #   tier0 tier1 tier1-pdf tier2-cf tier3-browser tier3b-hard expected-miss
    category: str
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    # If set, the result's source_method must match; mismatch is a warning.
    expected_tier: Optional[str] = None
    # True → a miss is acceptable (paywalled / impenetrable site).
    # The test still runs and reports the outcome; it just doesn't count as a failure.
    expect_failure_ok: bool = False


@dataclass
class TestResult:
    tc: TestCase
    success: bool              # engine returned markdown (not empty)
    source_method: Optional[str]
    latency_s: float
    md_len: int
    preview: str               # first 300 chars, whitespace-collapsed
    errors: list[str]          # quality problems
    cascade: list[dict]        # tier attempts from event log (may be empty)


# ── Test cases ─────────────────────────────────────────────────────────────────
#
# Ordered by difficulty. Each category maps to a protection class:
#
#  tier0          → direct open API (Wikimedia, ArXiv)  — expect <1s
#  tier1          → plain HTTP, no meaningful bot wall   — expect <3s
#  tier1-pdf      → PDF fetched over plain HTTP          — expect <5s
#  tier2-cf       → Cloudflare CDN / Bot Management      — expect tier1–tier3
#  tier3-browser  → JS-heavy or CF + rate limiting       — expect tier3+
#  tier3b-hard    → DataDome / PerimeterX / Kasada class — may miss
#  expected-miss  → paywall + aggressive bot detection   — miss is normal

TESTS: list[TestCase] = [

    # ── TIER 0: Direct open APIs — Wikipedia, ArXiv ────────────────────────────
    TestCase(
        url="https://en.wikipedia.org/wiki/Artificial_intelligence",
        label="Wikipedia — Artificial Intelligence article",
        category="tier0",
        expected_tier="tier_1",
        must_contain=["intelligence", "machine learning"],
        must_not_contain=["enable JavaScript", "Access Denied"],
    ),
    TestCase(
        url="https://arxiv.org/abs/2303.08774",
        label="ArXiv — GPT-4 Technical Report abstract",
        category="tier0",
        expected_tier="tier_1",
        must_contain=["GPT-4", "OpenAI"],
        must_not_contain=["enable JavaScript"],
    ),

    # ── TIER 1: Plain HTTP, no meaningful bot wall ─────────────────────────────
    TestCase(
        url="https://news.ycombinator.com",
        label="Hacker News — frontpage (vanilla HTML)",
        category="tier1",
        expected_tier="tier_2",
        must_contain=["Hacker News"],
        must_not_contain=["enable JavaScript", "Access Denied"],
    ),
    TestCase(
        url="https://apnews.com/hub/technology",
        label="AP News — Technology hub (public, no paywall)",
        category="tier1",
        must_contain=["AP News"],
        must_not_contain=["Just a moment", "Access Denied"],
    ),
    TestCase(
        url="https://arstechnica.com",
        label="Ars Technica — homepage",
        category="tier1",
        must_contain=["Ars Technica"],
        must_not_contain=["Just a moment", "enable JavaScript"],
    ),
    TestCase(
        url="https://www.ycombinator.com/jobs",
        label="Y Combinator — Jobs board (plain, open)",
        category="tier1",
        must_contain=["job", "engineer"],
        must_not_contain=["Access Denied", "Just a moment"],
    ),

    # ── TIER 1 PDF: binary extraction via pypdf ────────────────────────────────
    # Note: arxiv.org has a botwall winning-tier hint → tier_1, so the
    # orchestrator may serve the abstract via API instead of parsing the PDF.
    # The extraction is still correct; we just can't assert a specific tier.
    TestCase(
        url="https://arxiv.org/pdf/2303.08774",
        label="ArXiv — GPT-4 paper PDF (pypdf or API)",
        category="tier1-pdf",
        must_contain=["GPT-4"],
        must_not_contain=["Access Denied"],
    ),

    # ── TIER 2-CF: Cloudflare-protected news sites ─────────────────────────────
    # Cloudflare Bot Management or Turnstile; cloudscraper or browser may win.
    TestCase(
        url="https://www.theguardian.com/technology",
        label="The Guardian — Technology section (Cloudflare)",
        category="tier2-cf",
        must_contain=["Guardian"],
        must_not_contain=["Just a moment", "Checking your browser", "enable JavaScript"],
    ),
    TestCase(
        url="https://techcrunch.com",
        label="TechCrunch — homepage (Cloudflare Bot Management)",
        category="tier2-cf",
        must_contain=["TechCrunch"],
        must_not_contain=["Just a moment", "enable JavaScript"],
    ),
    TestCase(
        url="https://www.reuters.com/technology/artificial-intelligence/",
        label="Reuters — AI section (Cloudflare)",
        category="tier2-cf",
        must_contain=["Reuters"],
        must_not_contain=["Just a moment", "Access Denied"],
    ),

    # ── TIER 3-BROWSER: Heavy JS render or rate-limited ───────────────────────
    # Need patchright or camoufox to render.
    TestCase(
        url="https://www.reddit.com/r/technology/",
        label="Reddit — r/technology (CF + heavy JS SPA)",
        category="tier3-browser",
        must_contain=["technology", "reddit"],
        must_not_contain=["Just a moment", "Checking your browser"],
    ),
    TestCase(
        url="https://www.bbc.com/news/technology",
        label="BBC News — Technology (Cloudflare + JS)",
        category="tier3-browser",
        must_contain=["BBC"],
        must_not_contain=["Just a moment", "enable JavaScript"],
    ),
    TestCase(
        url="https://stackoverflow.com/questions/tagged/python",
        label="Stack Overflow — Python questions (Cloudflare)",
        category="tier3-browser",
        must_contain=["python", "question"],
        must_not_contain=["Just a moment", "Access Denied"],
    ),

    # ── TIER 3B-HARD: DataDome / PerimeterX / HUMAN ───────────────────────────
    # These require Camoufox (Firefox stealth) or fall through to Firecrawl.
    # A miss is informative and does not count as a test failure.
    TestCase(
        url="https://www.glassdoor.com/Overview/Working-at-OpenAI-EI_IE3198026.11,17.htm",
        label="Glassdoor — OpenAI company page (DataDome)",
        category="tier3b-hard",
        must_contain=["OpenAI"],
        must_not_contain=["automated access", "robot"],
        expect_failure_ok=True,
    ),
    TestCase(
        url="https://www.indeed.com/cmp/Openai",
        label="Indeed — OpenAI company reviews",
        category="tier3b-hard",
        must_contain=["OpenAI"],
        expect_failure_ok=True,
    ),
    TestCase(
        url="https://www.zillow.com/homes/for_sale/",
        label="Zillow — Homes for sale (PerimeterX/HUMAN)",
        category="tier3b-hard",
        must_contain=["home", "sale"],
        must_not_contain=["Access Denied"],
        expect_failure_ok=True,
    ),

    # ── EXPECTED MISS: Paywall + hardened bot detection ────────────────────────
    # Document the engine's ceiling. Miss is expected and correct behavior here.
    TestCase(
        url="https://www.bloomberg.com/technology",
        label="Bloomberg — Technology (Kasada + paywall)",
        category="expected-miss",
        must_contain=[],
        expect_failure_ok=True,
    ),
    TestCase(
        url="https://www.linkedin.com/jobs/",
        label="LinkedIn — Jobs (custom heavy bot detection)",
        category="expected-miss",
        must_contain=[],
        expect_failure_ok=True,
    ),
]

# ── Quality checks ──────────────────────────────────────────────────────────────

# Bot-wall strings that should never appear in successfully-extracted content.
# Keep this list narrow — false positives (e.g. "captcha" in a news article)
# are worse than false negatives.
_BOTWALL_PHRASES = [
    "just a moment...",
    "checking your browser",
    "please enable javascript",
    "access denied",
    "403 forbidden",
    "are you a human",
    "verify you are human",
]

def check_quality(tc: TestCase, markdown: str) -> list[str]:
    """Return a list of quality failure messages. Empty list = clean."""
    errors: list[str] = []
    md_lower = markdown.lower()

    for kw in tc.must_contain:
        if kw.lower() not in md_lower:
            errors.append(f"missing keyword: '{kw}'")

    for kw in tc.must_not_contain:
        if kw.lower() in md_lower:
            errors.append(f"unwanted string found: '{kw}'")

    for phrase in _BOTWALL_PHRASES:
        if phrase in md_lower:
            errors.append(f"bot-wall page leaked: '{phrase}'")

    if len(markdown) < 500:
        errors.append(f"suspiciously short: {len(markdown)} chars")

    # Relative URL leak: markdown links should be absolute after normalization
    relative_links = re.findall(r'\[.*?\]\((?!http)(?!#)(?!mailto)(/[^)]+)\)', markdown)
    if relative_links:
        examples = relative_links[:3]
        errors.append(f"relative URLs in output: {examples}")

    return errors


def tier_warning(tc: TestCase, source_method: str) -> Optional[str]:
    if tc.expected_tier and source_method != tc.expected_tier:
        return f"tier mismatch — expected {tc.expected_tier}, got {source_method}"
    return None


# ── Event log reader ────────────────────────────────────────────────────────────

def read_cascade(url: str, log_path: Path, since_pos: int) -> list[dict]:
    """Read tier attempts logged for `url` since byte offset `since_pos`."""
    if not log_path.exists():
        return []
    attempts: list[dict] = []
    try:
        with log_path.open() as f:
            f.seek(since_pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("url") == url:
                        attempts.append(ev)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return attempts


def _log_pos(log_path: Path) -> int:
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


# ── Runner ──────────────────────────────────────────────────────────────────────

def run_test(tc: TestCase, log_path: Path) -> TestResult:
    # Import lazily so --help is instant and arg parsing is fast
    from switchback import scrape as engine_scrape

    pos_before = _log_pos(log_path)
    t0 = time.time()

    try:
        results = engine_scrape([tc.url])
    except Exception as exc:
        return TestResult(
            tc=tc, success=False, source_method=None,
            latency_s=time.time() - t0, md_len=0, preview="",
            errors=[f"exception during scrape: {exc}"],
            cascade=read_cascade(tc.url, log_path, pos_before),
        )

    latency = time.time() - t0
    cascade = read_cascade(tc.url, log_path, pos_before)

    if not results:
        return TestResult(
            tc=tc, success=False, source_method=None,
            latency_s=latency, md_len=0, preview="",
            errors=["all tiers failed — no result returned"],
            cascade=cascade,
        )

    r = results[0]
    md = r.markdown or ""
    errors = check_quality(tc, md)

    tier_warn = tier_warning(tc, r.source_method)
    if tier_warn:
        errors.append(tier_warn)

    preview = re.sub(r"\s+", " ", md).strip()[:300]

    return TestResult(
        tc=tc, success=True,
        source_method=r.source_method,
        latency_s=latency, md_len=len(md),
        preview=preview, errors=errors,
        cascade=cascade,
    )


# ── Display ─────────────────────────────────────────────────────────────────────

def _status(result: TestResult) -> str:
    if not result.success:
        if result.tc.expect_failure_ok:
            return yellow("○ miss (expected)")
        return red("✗ MISS")
    if result.errors:
        return yellow("⚠ WARN")
    return green("✓ pass")


def _fmt_cascade(cascade: list[dict]) -> str:
    if not cascade:
        return dim("(no event log entries)")
    parts = []
    for ev in cascade:
        tier = ev.get("tier", "?")
        outcome = ev.get("outcome", "?")
        ms = ev.get("latency_ms")
        ms_str = f" {ms}ms" if ms else ""
        color = green if outcome == "ok" else (yellow if outcome == "short_content" else dim)
        parts.append(color(f"{tier}:{outcome}{ms_str}"))
    return " → ".join(parts)


def print_result(result: TestResult, verbose: bool) -> None:
    icon = _status(result)
    tier = cyan(result.source_method) if result.source_method else dim("—")
    lat = f"{result.latency_s:.1f}s"
    length = f"{result.md_len:,} chars" if result.md_len else dim("—")

    print(f"  {icon}  {bold(result.tc.label)}")
    print(f"       {dim('tier=')+tier}  {dim('latency=')}{lat}  {dim('len=')}{length}")

    for err in result.errors:
        print(f"       {yellow('⚠')} {err}")

    if verbose:
        if result.preview:
            print(f"       {dim('preview:')} {dim(result.preview[:200])}…")
        cascade_str = _fmt_cascade(result.cascade)
        if cascade_str:
            print(f"       {dim('cascade:')} {cascade_str}")

    print()


def print_summary(results: list[TestResult]) -> None:
    total = len(results)
    hard_failures = [r for r in results if not r.success and not r.tc.expect_failure_ok]
    quality_warns = [r for r in results if r.success and r.errors]
    clean_passes = [r for r in results if r.success and not r.errors]
    expected_misses = [r for r in results if not r.success and r.tc.expect_failure_ok]

    print("─" * 64)
    print(bold("Results"))
    print(f"  {green(f'{len(clean_passes)} passed')}  "
          f"{yellow(f'{len(quality_warns)} warnings')}  "
          f"{red(f'{len(hard_failures)} failed')}  "
          f"{dim(f'{len(expected_misses)} expected misses')}  "
          f"/ {total} total")
    print()

    if hard_failures:
        print(bold("  Failed:"))
        for r in hard_failures:
            print(f"    {red('✗')} {r.tc.label}")
        print()

    if quality_warns:
        print(bold("  Warnings:"))
        for r in quality_warns:
            print(f"    {yellow('⚠')} {r.tc.label}")
            for e in r.errors:
                print(f"      {dim(e)}")
        print()

    # Time spent per tier, aggregated across every cascade attempt — not just the
    # winning tier. This is where wall-clock actually goes: a tier that misses
    # still costs time before the cascade falls through to the next rung. Bucketing
    # the URL's total latency under only the winner (as this used to) overstated
    # the winner and hid the cascade overhead. Sourced from the event log — the
    # same per-tier timing the OTLP backend trace waterfall shows.
    by_tier_ms: dict[str, list[float]] = {}
    for r in results:
        for ev in r.cascade:
            tier = ev.get("tier")
            ms = ev.get("latency_ms")
            if tier and ms is not None:
                by_tier_ms.setdefault(tier, []).append(ms)

    if by_tier_ms:
        print(bold("  Time spent per tier (all attempts, not just the winner):"))
        tier_order = ["tier_1", "tier_2", "tier_3",
                      "tier_4", "tier_5", "tier_6",
                      "tier_7"]
        ordered = sorted(by_tier_ms.items(), key=lambda kv: (
            tier_order.index(kv[0]) if kv[0] in tier_order else 99))
        for tier, ms_list in ordered:
            avg = sum(ms_list) / len(ms_list) / 1000
            mn = min(ms_list) / 1000
            mx = max(ms_list) / 1000
            bar = "▪" * min(int(avg), 20)
            print(f"    {tier:<24} avg={avg:.1f}s  min={mn:.1f}s  max={mx:.1f}s  n={len(ms_list)}  {dim(bar)}")
    print()

    # Success rate by category
    cats: dict[str, tuple[int, int]] = {}
    for r in results:
        cat = r.tc.category
        ok, total_c = cats.get(cat, (0, 0))
        cats[cat] = (ok + (1 if r.success else 0), total_c + 1)

    print(bold("  Success rate by category:"))
    for cat, (ok, tot) in cats.items():
        pct = ok / tot * 100
        bar = green("█" * ok) + dim("░" * (tot - ok))
        print(f"    {cat:<22} {ok}/{tot}  ({pct:.0f}%)  {bar}")
    print()


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scraper engine evaluation suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--quick", action="store_true",
                        help="Run tier0 + tier1 only (skip slow browser tiers)")
    parser.add_argument("--filter", metavar="CAT",
                        help="Only run tests whose category starts with CAT "
                             "(e.g. tier0, tier2-cf, tier3b-hard)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show markdown preview and cascade path per test")
    parser.add_argument("--deadline", type=int, default=None,
                        help="Override SCRAPER_DEADLINE_S for this run")
    args = parser.parse_args()

    if args.deadline:
        os.environ["SCRAPER_DEADLINE_S"] = str(args.deadline)

    tests = list(TESTS)
    if args.quick:
        tests = [t for t in tests if t.category in ("tier0", "tier1", "tier1-pdf")]
        print(dim("  --quick: running tier0 + tier1 only\n"))
    if args.filter:
        tests = [t for t in tests if t.category.startswith(args.filter)]
        print(dim(f"  --filter {args.filter!r}: {len(tests)} tests selected\n"))

    if not tests:
        print(red("No tests match — check your --filter value."))
        print("Available categories:", ", ".join(sorted({t.category for t in TESTS})))
        return 1

    # Event log path (matches botwall default)
    log_path = Path(__file__).parent / "state" / "botwall_events.jsonl"

    results: list[TestResult] = []
    last_cat: Optional[str] = None

    for tc in tests:
        if tc.category != last_cat:
            last_cat = tc.category
            label = tc.category.upper().replace("-", " ")
            print(bold(f"\n── {label} {'─' * (50 - len(label))}"))

        print(f"  {dim('→')} {tc.url}")
        result = run_test(tc, log_path)
        results.append(result)
        print_result(result, verbose=args.verbose)

    print_summary(results)

    failures = [r for r in results if not r.success and not r.tc.expect_failure_ok]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
