"""Periodic flagging — surface the things worth a human glance, on a schedule.

Requirement 13: "the system should flag some of these details from time to
time." This isn't a daemon — it's a single pass over the metrics rollup that
emits a digest to the logger (which ships to the OTLP backend when setup_logs() is on).
Run it from cron / the /loop skill / any scheduler:

    python -m switchback.flags                 # text digest to stdout + logs
    python -m switchback.flags --minutes 60    # only the last hour
    python -m switchback.flags --json          # machine-readable digest

What it flags:
  • domains still landing on paid Firecrawl (winning_tier == tier4_firecrawl)
  • domains escalated to residential egress (needs_egress)
  • domains throwing the most bot-wall challenges (by vendor)
  • low coverage / negative cost savings in the window
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

from .reporting import build_report
from .tracing import setup_logs

logger = logging.getLogger(__name__)

# A domain is "stuck" if its winning tier is the paid one — these are the hosts
# that still cost money and are the prime targets for a new tier / cookie / rule.
_PAID_TIER = "tier4_firecrawl"


def build_digest(minutes: int | None = None) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes) if minutes else None
    rep = build_report(since=since)
    domains = rep["domains"]

    stuck = sorted(
        (h for h, d in domains.items() if d.get("winning_tier") == _PAID_TIER),
        key=lambda h: -domains[h]["attempts"],
    )
    egress = [h for h, d in domains.items() if d.get("needs_egress")]
    challengers = sorted(
        ((h, d["challenges"]) for h, d in domains.items() if d.get("challenges")),
        key=lambda kv: -sum(kv[1].values()),
    )
    return {
        "window_minutes": minutes,
        "coverage": rep["coverage"],
        "cost": rep["cost"],
        "stuck_on_firecrawl": stuck,
        "needs_egress": egress,
        "top_challenged": [{"host": h, "challenges": c} for h, c in challengers[:10]],
    }


def emit(digest: dict) -> None:
    """Log the noteworthy parts at WARNING so they surface in the OTLP backend."""
    cov, cost = digest["coverage"], digest["cost"]
    logger.info("flags: coverage %.1f%% (%d/%d urls), savings $%.4f (%.1f%%)",
                cov["success_pct"], cov["succeeded"], cov["unique_urls"],
                cost["savings_usd"], cost["savings_pct"])
    if digest["stuck_on_firecrawl"]:
        logger.warning("flags: %d domain(s) still on paid Firecrawl: %s",
                       len(digest["stuck_on_firecrawl"]),
                       ", ".join(digest["stuck_on_firecrawl"][:20]))
    if digest["needs_egress"]:
        logger.warning("flags: %d domain(s) escalated to residential egress: %s",
                       len(digest["needs_egress"]), ", ".join(digest["needs_egress"][:20]))
    for item in digest["top_challenged"]:
        logger.info("flags: %s challenges %s", item["host"], item["challenges"])
    if cost["savings_usd"] < 0:
        logger.warning("flags: NEGATIVE savings ($%.4f) — engine cost exceeds the "
                       "Firecrawl baseline in this window", cost["savings_usd"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, help="Only consider the last N minutes")
    ap.add_argument("--json", action="store_true", help="Emit the digest as JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    setup_logs()  # ship the digest to the OTLP backend too when configured
    digest = build_digest(args.minutes)
    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        emit(digest)


if __name__ == "__main__":
    main()
