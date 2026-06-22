"""Process-wide cap on simultaneous headless browsers.

The browser tiers (patchright ~150MB, Camoufox ~600MB) are the memory-heavy
rungs. The engine scrapes sequentially today, but when callers run scrapes in
parallel this semaphore bounds how many browsers spin up at once. Default 1
matches the sequential design (one browser, one footprint); raise it with
SCRAPER_BROWSER_CONCURRENCY once you know the box has headroom.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

MAX_BROWSER_CONCURRENCY = max(1, int(os.getenv("SCRAPER_BROWSER_CONCURRENCY", "1")))
_sem = threading.BoundedSemaphore(MAX_BROWSER_CONCURRENCY)


@contextmanager
def browser_slot(label: str = "browser"):
    """Acquire one of the MAX_BROWSER_CONCURRENCY browser slots for the duration
    of a browser launch; blocks if all slots are in use. Logs the wait when a
    caller actually had to queue (a signal the cap is saturated)."""
    t0 = time.monotonic()
    _sem.acquire()
    waited = time.monotonic() - t0
    if waited > 0.1:
        logger.info(f"{label}: waited {waited * 1000:.0f}ms for a browser slot "
                    f"(SCRAPER_BROWSER_CONCURRENCY={MAX_BROWSER_CONCURRENCY})")
    try:
        yield
    finally:
        _sem.release()
