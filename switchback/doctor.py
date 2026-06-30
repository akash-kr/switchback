"""Preflight readiness check — `switchback doctor`.

Reports which tiers can actually run on this box and, when one can't, the exact
fix. Built for cold-start deploys (e.g. Azure) where the stealth browser is
installed by a background thread *after* boot: run this to confirm the tiers are
live before sending traffic, or to see why Tier 2/3 aren't catching anything.

Exit code: 0 if both capable local tiers (cloudscraper + browser) are ready,
else 1 — so it doubles as a healthcheck.
"""
from __future__ import annotations

import os
import shutil

from .tiers import tier_3, tier_4


def _camoufox() -> tuple[bool, str]:
    if os.getenv("SCRAPER_DISABLE_CAMOUFOX"):
        return False, "off (SCRAPER_DISABLE_CAMOUFOX set)"
    try:
        import camoufox  # noqa: F401
    except ImportError:
        return False, 'not installed — pip install "switchback[camoufox]" && camoufox fetch'
    return True, "camoufox installed"


def probe() -> list[tuple[str, bool, str]]:
    """(label, ok, detail) for each tier/dependency that matters at runtime."""
    cs_ok, cs_detail = tier_3.available()
    br_ok, br_detail = tier_4.available()
    node = shutil.which("node")
    return [
        ("tier_3 (cloudscraper)", cs_ok, cs_detail),
        ("tier_4 (browser)", br_ok, br_detail),
        ("tier_5 (camoufox)", *_camoufox()),
        ("node (tier_3 v3 concurrency)", bool(node),
         node or "not on PATH — tier_3 falls back to slower, thread-fragile js2py"),
        ("tier_7 (firecrawl)", bool(os.getenv("FIRECRAWL_API_KEY")),
         "FIRECRAWL_API_KEY set" if os.getenv("FIRECRAWL_API_KEY")
         else "off (no FIRECRAWL_API_KEY)"),
    ]


def report() -> int:
    rows = probe()
    print("switchback doctor — tier readiness\n")
    for label, ok, detail in rows:
        mark = "OK  " if ok else "MISS"
        print(f"  [{mark}] {label:30} {detail}")
    cs_ok = rows[0][1]
    br_ok = rows[1][1]
    if cs_ok and br_ok:
        print("\nCapable tiers ready.")
        return 0
    print("\nOne or more capable tiers are unavailable (see above). On a cold "
          "start this may resolve once the async install thread finishes.")
    return 1
