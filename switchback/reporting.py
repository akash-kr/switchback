"""Metrics rollups from the engine's own state — no external store needed.

Reads the two files the policy already writes (see switchback.policy.botwall):

  state/botwall_events.jsonl  — one row per *tier attempt*:
      {ts, url, host, tier, outcome, md_len, latency_ms, error, status_code, challenge}
  state/botwall_db.json       — per-host record incl. challenge_counts, winning_tier

and produces the metrics the firecrawl-replacement case is argued on:

  • cost savings vs Firecrawl (free-tier wins are money not spent; hard pages
    Firecrawl bills more credits for are weighted by HARD_MULT)
  • latency — overall and per tier and per domain (mean/median/min/max/p50/p95)
  • coverage (unique-URL success rate)
  • error codes by domain
  • challenges / bot-walls by domain

Pure functions returning JSON-serialisable dicts, so the same rollup backs the
CLI (scrape_stats.py), the HTTP API (/metrics), and the periodic digest
(switchback.flags). One tier attempt ≈ sequential wall-clock, so a URL's total
latency is approximated by summing its attempts' latencies.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median

from .policy import botwall

# Cost model (estimates — override to your real Firecrawl rates).
#   FIRECRAWL_USD   $ per basic Firecrawl scrape (matches benchmark.py's default)
#   HARD_MULT       credit multiplier Firecrawl charges for stealth/JS-rendered
#                   pages — the hard ones our browser/residential tiers resolve
#                   for free. Firecrawl's stealth proxy is ~5× basic, hence 5.
FIRECRAWL_USD = float(os.getenv("BENCH_FIRECRAWL_USD", "0.001"))
HARD_MULT = float(os.getenv("BENCH_FIRECRAWL_HARD_MULT", "5"))

# Tiers whose win means Firecrawl would have billed the hard (stealth) rate.
_HARD_TIERS = {"tier_4", "tier_5", "tier_6", "tier_7"}


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * p / 100)))
    return s[idx]


def _stats(values: list[int]) -> dict:
    """min/max/mean/median/p50/p95 over a list of latencies (ms)."""
    if not values:
        return {"count": 0, "min": 0, "max": 0, "mean": 0, "median": 0, "p50": 0, "p95": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(mean(values)),
        "median": round(median(values)),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
    }


def load_events(path: str | None = None, since: datetime | None = None) -> list[dict]:
    """Read botwall_events.jsonl (optionally only rows at/after `since`)."""
    path = path or botwall.EVENTS_PATH
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                dt = _parse_ts(e.get("ts", ""))
                if dt is None or dt < since:
                    continue
            out.append(e)
    return out


def _by_url(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        out[e.get("url") or "?"].append(e)
    return out


def _url_total_ms(attempts: list[dict]) -> int:
    """Per-URL wall-clock proxy: tiers run sequentially, so sum the attempts."""
    return sum(a["latency_ms"] for a in attempts if isinstance(a.get("latency_ms"), int))


def _coverage(by_url: dict[str, list[dict]]) -> dict:
    succeeded = {u: a for u, a in by_url.items()
                 if any(e.get("outcome") == "ok" for e in a)}
    total = len(by_url)
    return {
        "unique_urls": total,
        "succeeded": len(succeeded),
        "failed": total - len(succeeded),
        "success_pct": round(100 * len(succeeded) / total, 1) if total else 0.0,
    }


def _per_tier(events: list[dict]) -> dict:
    tiers: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        tiers[e.get("tier") or "?"].append(e)
    out = {}
    for tier, evs in tiers.items():
        ok = [e for e in evs if e.get("outcome") == "ok"]
        lats = [e["latency_ms"] for e in ok if isinstance(e.get("latency_ms"), int)]
        out[tier] = {
            "attempts": len(evs),
            "ok": len(ok),
            "miss": len(evs) - len(ok),
            "ok_pct": round(100 * len(ok) / len(evs), 1) if evs else 0.0,
            "latency_ms": _stats(lats),
        }
    return out


def _outcomes(events: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        counts[e.get("outcome") or "?"] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _cost(by_url: dict[str, list[dict]]) -> dict:
    """Engine spend vs the Firecrawl-everything baseline curiouscats pays today.

    For each unique URL:
      engine_cost           = FIRECRAWL_USD per actual Firecrawl invocation
      firecrawl_equivalent  = what scraping that URL via Firecrawl would cost —
                              HARD_MULT× when it needed a hard tier (Firecrawl
                              bills stealth/JS pages more), else 1×.
    savings = baseline − engine. Failed URLs still count toward the baseline
    (Firecrawl would have been billed for the attempt too)."""
    engine = 0.0
    baseline = 0.0
    firecrawl_calls = 0
    for attempts in by_url.values():
        used_fc = sum(1 for a in attempts if a.get("outcome") == "firecrawl_used")
        firecrawl_calls += used_fc
        engine += used_fc * FIRECRAWL_USD
        hard = any((a.get("tier") in _HARD_TIERS) or a.get("challenge") for a in attempts)
        baseline += FIRECRAWL_USD * (HARD_MULT if hard else 1)
    return {
        "firecrawl_usd_per_scrape": FIRECRAWL_USD,
        "hard_multiplier": HARD_MULT,
        "firecrawl_invocations": firecrawl_calls,
        "engine_cost_usd": round(engine, 4),
        "firecrawl_baseline_usd": round(baseline, 4),
        "savings_usd": round(baseline - engine, 4),
        "savings_pct": round(100 * (baseline - engine) / baseline, 1) if baseline else 0.0,
    }


def _domains(events: list[dict], by_url: dict[str, list[dict]], db: dict) -> dict:
    """Per-host (== per-subdomain) rollup: attempts, error codes, challenges,
    latency. Challenges come from the durable per-host counts in botwall_db.json
    so they reflect all history, not just the events window."""
    host_attempts: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        host_attempts[e.get("host") or "?"].append(e)

    # Per-URL total latency grouped by host (host taken from the URL's events).
    host_url_ms: dict[str, list[int]] = defaultdict(list)
    for attempts in by_url.values():
        host = next((a.get("host") for a in attempts if a.get("host")), "?")
        host_url_ms[host].append(_url_total_ms(attempts))

    hosts_db = db.get("hosts", {})
    out = {}
    for host, evs in host_attempts.items():
        error_codes: dict[str, int] = defaultdict(int)
        for e in evs:
            sc = e.get("status_code")
            if sc:
                error_codes[str(sc)] += 1
        rec = hosts_db.get(host, {})
        out[host] = {
            "attempts": len(evs),
            "ok": sum(1 for e in evs if e.get("outcome") == "ok"),
            "winning_tier": rec.get("winning_tier"),
            "needs_egress": bool(rec.get("needs_egress")),
            "error_codes": dict(error_codes),
            "challenges": dict(rec.get("challenge_counts", {})),
            "latency_ms": _stats(host_url_ms.get(host, [])),
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["attempts"]))


def build_report(events: list[dict] | None = None, db: dict | None = None,
                 since: datetime | None = None) -> dict:
    """Full metrics rollup. Reads state files when args are omitted."""
    if events is None:
        events = load_events(since=since)
    if db is None:
        db = botwall.load_db()
    by_url = _by_url(events)
    url_totals = [_url_total_ms(a) for a in by_url.values()]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": len(events),
        "coverage": _coverage(by_url),
        "cost": _cost(by_url),
        "latency_overall_ms": _stats(url_totals),
        "latency_per_tier": _per_tier(events),
        "outcomes": _outcomes(events),
        "domains": _domains(events, by_url, db),
    }


def domain_report(since: datetime | None = None) -> dict:
    """Just the per-domain table (error codes + challenges + latency per host)."""
    return build_report(since=since)["domains"]
