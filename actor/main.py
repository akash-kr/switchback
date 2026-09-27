"""Apify Actor entry point: switchback's scrape cascade behind the Apify input/dataset.

Thin wrapper — all scraping logic lives in the `switchback` package. This file only
maps Actor input to `scrape_detailed`, pushes one dataset row per URL, and charges
pay-per-event by the tier that actually produced the page:

    page-light    tier_1–tier_3 success (API mirror / HTTP / anti-bot solver)
    page-browser  tier_4 success (stealth Chromium)
    (no charge)   any failure — the row is still pushed so the user sees why

Per-site memory (which tier wins on which host) is botwall_db.json. It is restored
from and saved to a named key-value store in the *user's* account, so repeat runs
start each host at the tier that worked last time.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

from apify import Actor

STATE_DIR = pathlib.Path("/tmp/switchback-state")
MEMORY_STORE = "switchback-memory"
MEMORY_KEY = "botwall_db"
LIGHT_TIERS = {"tier_1", "tier_2", "tier_3"}

# The Actor ships tiers 1–4 only (HTTP + stealth Chromium). Set before importing
# switchback: its config is read from the environment at import time.
os.environ.setdefault("SCRAPER_STATE_DIR", str(STATE_DIR))
os.environ.setdefault("SCRAPER_DISABLE_CAMOUFOX", "1")
os.environ.setdefault("SCRAPER_DISABLE_FIRECRAWL", "1")
# Failed pages are free to the user but still burn compute: cap the anti-bot
# solver so an unsolvable challenge falls through to the browser quickly.
os.environ.setdefault("SCRAPER_TIER_3_TIMEOUT_S", "12")


def _event_for(tier: str | None) -> str:
    return "page-light" if tier in LIGHT_TIERS else "page-browser"


def _row(o) -> dict:
    return {
        "url": o.url,
        "ok": o.ok,
        "markdown": o.markdown if o.ok else None,
        "tier": o.source_method if o.ok else None,
        "outcome": o.final_outcome,
        "errorClass": o.error_class,
        "statusCode": o.status_code,
        "latencyMs": o.latency_ms,
        # Tiers switched off in this Actor (5–7) would only read as noise here.
        "attempts": [{"tier": a.tier, "outcome": a.outcome}
                     for a in (o.attempts or []) if a.outcome != "disabled"],
    }


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}
        urls = [s["url"] for s in inp.get("startUrls", []) if s.get("url")]
        fmt = inp.get("outputFormat", "markdown")
        remember = inp.get("rememberSites", True)
        if not urls:
            await Actor.fail(status_message="No URLs given: add at least one to Start URLs.")
            return

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        memory = await Actor.open_key_value_store(name=MEMORY_STORE) if remember else None
        if memory:
            saved = await memory.get_value(MEMORY_KEY)
            if saved:
                (STATE_DIR / "botwall_db.json").write_text(json.dumps(saved))

        from switchback import scrape_detailed  # after env + state are in place

        charging = Actor.get_charging_manager()
        ok = 0
        for i, url in enumerate(urls, 1):
            # Don't scrape a page the user's max-cost-per-run can't pay for.
            if charging.is_event_charge_limit_reached("page-light"):
                Actor.log.info("Max cost per run reached; stopping early.")
                break
            await Actor.set_status_message(f"Scraping {i}/{len(urls)}: {url}")
            [outcome] = await asyncio.to_thread(scrape_detailed, [url], fmt)
            row = _row(outcome)
            if outcome.ok:
                ok += 1
                result = await Actor.push_data(row, charged_event_name=_event_for(outcome.source_method))
            else:
                result = await Actor.push_data(row)
            # Respect the user's max-cost-per-run: the SDK stops charging silently,
            # so stop scraping too rather than burn compute on pages we can't bill.
            if result and result.event_charge_limit_reached:
                Actor.log.info("Max cost per run reached; stopping early.")
                break

        if memory:
            db = STATE_DIR / "botwall_db.json"
            if db.exists():
                await memory.set_value(MEMORY_KEY, json.loads(db.read_text()))

        await Actor.set_status_message(f"Done: {ok}/{len(urls)} pages scraped.", is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
