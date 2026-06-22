"""Botwall v2 — adaptive per-host + per-URL routing policy.

Ported from musings' botwall, with one addition: it records *which tier wins*
per host (not just failures), so the orchestrator can start a known-hard host at
its winning tier instead of replaying tiers that always miss.

Skip granularity
----------------
- Host-level skip: only for seeded hard-block domains and manual overrides.
  Auto-promotion never elevates an entire domain to skip.
- URL-level skip: individual articles/paths are excluded after PROMOTE_URL_AFTER
  consecutive hard failures (botwall hit or short content), so one bad URL never
  taints its whole domain.

State files (all in SCRAPER_STATE_DIR):
  botwall_db.json       — host + URL records (authoritative state)
  botwall_events.jsonl  — every scrape outcome (machine-readable audit trail)
  botwall_excluded.jsonl — every URL-level exclusion event (machine-readable)
  botwall_excluded.log  — same, human-readable one-liner per exclusion
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

from .gates import host_of

logger = logging.getLogger(__name__)
_DB_WRITE_LOCK = threading.Lock()

_DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "state")
_STATE_DIR = os.getenv("SCRAPER_STATE_DIR", _DEFAULT_STATE_DIR)
os.makedirs(_STATE_DIR, exist_ok=True)
DB_PATH = os.path.join(_STATE_DIR, "botwall_db.json")
EVENTS_PATH = os.path.join(_STATE_DIR, "botwall_events.jsonl")
EXCLUDED_JSONL = os.path.join(_STATE_DIR, "botwall_excluded.jsonl")
EXCLUDED_LOG = os.path.join(_STATE_DIR, "botwall_excluded.log")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_SKIP_URLS_FILE = os.path.join(_PROJECT_ROOT, "config", "botwall_skip_urls.txt")
SKIP_URLS_FILE = os.getenv("SCRAPER_BOTWALL_SKIP_URLS_FILE", _DEFAULT_SKIP_URLS_FILE)

# Hosts known to hard-block; seeded on first run, then self-maintained.
SEED_HOSTS = {
    "www.sciencedirect.com": "seed: Cloudflare 1015",
    "sciencedirect.com": "seed: Cloudflare 1015",
    "linkinghub.elsevier.com": "seed: redirects to sciencedirect",
    "onlinelibrary.wiley.com": "seed: Cloudflare",
    "www.tandfonline.com": "seed: Cloudflare",
    "tandfonline.com": "seed: Cloudflare",
}

# ── Config (all overridable via env vars) ─────────────────────────────────────
#
# SCRAPER_BOTWALL_URL_SKIP_AFTER   int  ≥1, default 2
#   Hard failures (botwall/short-content) on the *same URL* before that URL is
#   excluded. Set to 0 to disable URL-level auto-exclusion entirely.
#
# SCRAPER_BOTWALL_DOMAIN_SKIP_AFTER  int  ≥1, default 0 (disabled)
#   Hard failures across *any* URLs on the same domain before the whole domain is
#   skip-listed. 0 (default) means domains are never auto-skipped — only seeded
#   hard-block domains and manual overrides are domain-level skips.
#
# SCRAPER_BOTWALL_COUNT_FIRECRAWL  bool  default false
#   When true, each time Firecrawl is invoked for a host it counts as a failure
#   toward the domain skip threshold (original v1 behaviour). No effect if
#   SCRAPER_BOTWALL_DOMAIN_SKIP_AFTER is 0.

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning(f"botwall: invalid {name}; using {default}")
        return default

PROMOTE_URL_AFTER    = _int_env("SCRAPER_BOTWALL_URL_SKIP_AFTER",    2)
PROMOTE_DOMAIN_AFTER = _int_env("SCRAPER_BOTWALL_DOMAIN_SKIP_AFTER", 0)
COUNT_FIRECRAWL      = os.getenv("SCRAPER_BOTWALL_COUNT_FIRECRAWL", "").lower() in ("1", "true", "yes")

# Hours an auto-skipped URL stays excluded before it's re-tested (self-healing).
# 0 = never re-test (legacy permanent skip). After the cooldown the URL is tried
# again; if it fails the cooldown re-stamps and its host is flagged needs_egress
# so the next attempt routes through the residential tier instead of giving up.
URL_SKIP_COOLDOWN_H  = _int_env("SCRAPER_BOTWALL_URL_SKIP_COOLDOWN_H", 24)

# Host-level egress escalation: after this many egress-worthy failures at the
# *local* tiers, the host is flagged needs_egress so future attempts route to the
# residential tier. 0 disables escalation.
PROMOTE_EGRESS_AFTER = _int_env("SCRAPER_BOTWALL_EGRESS_AFTER", 2)

# Outcomes a *single URL* is excluded for (deterministic per-URL failures). A 429
# is deliberately excluded — it's transient, so it escalates egress but never
# permanently skips the URL.
_URL_SKIP_OUTCOMES = ("botwall", "short_content", "http_block")

# Outcomes that mean "this IP/identity is the problem" → escalate to residential
# egress. Includes the transient 429 (a different IP dodges the rate limit).
_EGRESS_OUTCOMES = ("botwall", "short_content", "http_block", "rate_limited")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(ts: str | None) -> float | None:
    """Seconds since an ISO timestamp, or None if missing/unparseable."""
    if not ts:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return None


def _new_record(reason="", status="allow") -> dict:
    now = _now()
    return {
        "status": status, "reason": reason,
        "winning_tier": None,
        "tier_stats": {},       # {tier: {ok, miss}}
        "total_attempts": 0, "successes": 0,
        "first_seen": now, "last_event": now, "manual_override": None,
    }



def _parse_skip_urls_file(path: str) -> dict[str, str]:
    """Return {url: reason} from a skip-urls config file."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if " #" in line:
                    url, _, reason = line.partition(" #")
                    out[url.strip()] = reason.strip()
                else:
                    out[line] = "manual: botwall_skip_urls.txt"
    except Exception as e:
        logger.warning(f"botwall: could not read {path}: {e}")
    return out


def load_db() -> dict:
    db = {"version": 2, "updated_at": "", "hosts": {}, "urls": {}}
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH) as f:
                db = json.load(f)
        except Exception as e:
            logger.error(f"botwall: load failed ({e}); starting fresh")
    hosts = db.setdefault("hosts", {})
    urls = db.setdefault("urls", {})
    changed = False
    for host, reason in SEED_HOSTS.items():
        if host not in hosts:
            hosts[host] = _new_record(reason=reason, status="skip")
            changed = True
    for url, reason in _parse_skip_urls_file(SKIP_URLS_FILE).items():
        if url not in urls:
            now = _now()
            urls[url] = {"status": "skip", "reason": f"seed: {reason}",
                         "failures": 0, "first_seen": now, "last_event": now}
            changed = True
    if changed or not os.path.exists(DB_PATH):
        save_db(db)
    return db


def save_db(db: dict) -> None:
    db["updated_at"] = _now()
    tmp = f"{DB_PATH}.tmp.{os.getpid()}.{threading.get_ident()}"
    with _DB_WRITE_LOCK:
        with open(tmp, "w") as f:
            json.dump(db, f, indent=2, sort_keys=True)
        os.replace(tmp, DB_PATH)


def is_skipped(host: str, db: dict) -> bool:
    rec = db.get("hosts", {}).get(host)
    if not rec:
        return False
    if rec.get("manual_override") == "allow":
        return False
    if rec.get("manual_override") == "skip":
        return True
    return rec.get("status") == "skip"


def winning_tier(host: str, db: dict) -> str | None:
    rec = db.get("hosts", {}).get(host)
    return rec.get("winning_tier") if rec else None


def needs_egress(host: str, db: dict) -> bool:
    """True when the local tiers have repeatedly walled this host, so future
    attempts should escalate to the residential-egress tier."""
    rec = db.get("hosts", {}).get(host)
    return bool(rec and rec.get("needs_egress"))


def _log_event(url, tier, outcome, md_len=None, error=None, latency_ms=None,
               status_code=None, challenge=None) -> None:
    ev = {"ts": _now(), "url": url, "host": host_of(url), "tier": tier,
          "outcome": outcome, "md_len": md_len, "latency_ms": latency_ms,
          "error": error, "status_code": status_code, "challenge": challenge}
    try:
        with open(EVENTS_PATH, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception as e:
        logger.warning(f"botwall: event log failed: {e}")


def log_final(url: str, outcome: str, latency_ms=None, error=None,
              status_code=None) -> None:
    """Write one aggregate event for a URL's final cascade result (all_failed /
    deadline_exceeded / *_skipped). Makes 'why did this URL ultimately fail'
    a first-class row instead of something you reconstruct by grouping per-tier
    events. tier is logged as '<cascade>' to distinguish it."""
    _log_event(url, "<cascade>", outcome, error=error, latency_ms=latency_ms,
               status_code=status_code)


def is_url_skipped(url: str, db: dict) -> bool:
    """True when this URL is currently excluded.

    Auto-skips decay: after URL_SKIP_COOLDOWN_H the URL is re-tested (returns
    False) so a host that recovers self-heals. Seeded/manual skips and a
    cooldown of 0 stay permanent (legacy behaviour)."""
    rec = db.get("urls", {}).get(url)
    if not (rec and rec.get("status") == "skip"):
        return False
    reason = str(rec.get("reason", ""))
    if reason.startswith(("seed:", "manual:")) or not URL_SKIP_COOLDOWN_H:
        return True
    age = _age_seconds(rec.get("last_event"))
    return age is not None and age < URL_SKIP_COOLDOWN_H * 3600


def _log_exclusion(url: str, reason: str) -> None:
    """Write to both the structured JSONL and the human-readable log."""
    ev = {"ts": _now(), "url": url, "host": host_of(url), "reason": reason}
    try:
        with open(EXCLUDED_JSONL, "a") as f:
            f.write(json.dumps(ev) + "\n")
        with open(EXCLUDED_LOG, "a") as f:
            f.write(f"{ev['ts']}  EXCLUDED  {url}  [{reason}]\n")
    except Exception as e:
        logger.warning(f"botwall: exclusion log failed: {e}")


def _track_url_failure(url: str, outcome: str, db: dict) -> None:
    """Increment per-URL failure counter; exclude the URL when threshold is hit.

    Counts the deterministic per-URL failures (botwall / short_content / a hard
    403/401 http_block). Transient outcomes (rate limits, timeouts, network) do
    not accumulate toward a permanent exclusion.
    """
    if outcome not in _URL_SKIP_OUTCOMES:
        return
    urls = db.setdefault("urls", {})
    rec = urls.get(url)
    if rec is None:
        rec = {"status": "allow", "failures": 0, "reason": "",
               "first_seen": _now(), "last_event": _now()}
        urls[url] = rec
    rec["failures"] += 1
    rec["last_event"] = _now()  # re-stamp: extends the cooldown window
    if rec.get("status") == "skip":
        # Failed again on a post-cooldown re-test — the local tiers can't clear
        # this. Stay skipped (cooldown re-stamped above) and escalate the host's
        # egress so the next attempt routes through the residential tier.
        _mark_needs_egress(host_of(url), db)
        return
    if rec["failures"] >= PROMOTE_URL_AFTER:
        rec["status"] = "skip"
        rec["reason"] = f"auto: {rec['failures']}× {outcome}"
        logger.info(f"botwall excluded URL: {url} ({rec['reason']})")
        _log_exclusion(url, rec["reason"])


def _mark_needs_egress(host: str, db: dict) -> None:
    """Flag a host so future attempts start at the residential-egress tier."""
    rec = db.get("hosts", {}).get(host)
    if rec is not None and not rec.get("needs_egress"):
        rec["needs_egress"] = True
        logger.info(f"botwall: host flagged needs_egress (local tiers walled): {host}")


def _track_egress(host: str, tier: str, outcome: str, db: dict) -> None:
    """Escalate a host to residential egress after PROMOTE_EGRESS_AFTER
    egress-worthy failures at the *local* tiers.

    This is the fix for hard HTTP blocks: a 403/401/429 raises (→ http_block /
    rate_limited) and so never tripped the old botwall/short_content-only path,
    leaving the datacenter-IP-blocked hosts that residential egress is *for*
    unescalated. We don't count the residential tier's own misses (circular)."""
    if not PROMOTE_EGRESS_AFTER or outcome not in _EGRESS_OUTCOMES:
        return
    if tier == "tier_residential":
        return
    rec = db.get("hosts", {}).get(host)
    if rec is None or rec.get("needs_egress"):
        return
    rec["egress_failures"] = rec.get("egress_failures", 0) + 1
    if rec["egress_failures"] >= PROMOTE_EGRESS_AFTER:
        _mark_needs_egress(host, db)


def _clear_url_skip(url: str, db: dict) -> None:
    """Self-heal: a previously-excluded URL just succeeded, so un-skip it."""
    rec = db.get("urls", {}).get(url)
    if rec and rec.get("status") == "skip":
        rec.update(status="allow", failures=0, reason="", last_event=_now())
        logger.info(f"botwall: URL skip cleared after success: {url}")


def _track_domain_failure(host: str, outcome: str, db: dict) -> None:
    """Optionally auto-skip a domain after PROMOTE_DOMAIN_AFTER hard failures.

    Only active when SCRAPER_BOTWALL_DOMAIN_SKIP_AFTER > 0.
    """
    if not PROMOTE_DOMAIN_AFTER:
        return
    counts_as_failure = outcome in ("botwall", "short_content") or (
        COUNT_FIRECRAWL and outcome == "firecrawl_used"
    )
    if not counts_as_failure:
        return
    rec = db["hosts"].get(host)
    if not rec or rec.get("manual_override") or rec.get("status") == "skip":
        return
    rec.setdefault("domain_failures", 0)
    rec["domain_failures"] += 1
    if rec["domain_failures"] >= PROMOTE_DOMAIN_AFTER:
        rec["status"] = "skip"
        rec["reason"] = f"auto: {rec['domain_failures']}× domain {outcome}"
        logger.info(f"botwall domain skip-listed: {host} ({rec['reason']})")
        _log_exclusion(f"domain:{host}", rec["reason"])


def record(db: dict, url: str, tier: str, outcome: str, md_len=None, error=None,
           latency_ms=None, status_code=None, challenge=None) -> None:
    """Update host counters + per-tier stats + winning_tier; track URL-, egress-,
    and (optionally) domain-level failures; log event.

    outcome ∈ {ok, short_content, botwall, http_block, rate_limited, timeout,
    connection, http_error, error, firecrawl_used}.

    `challenge` names the bot-wall vendor (cloudflare / datadome / akamai / …)
    when one was served. Counts accumulate per host (the host key is the full
    FQDN, so this is already per-subdomain); domain-level rollups are derived in
    the reporting layer.
    """
    host = host_of(url)
    if not host:
        return
    hosts = db.setdefault("hosts", {})
    rec = hosts.get(host) or _new_record()
    rec["total_attempts"] += 1
    rec["last_event"] = _now()

    if challenge:
        counts = rec.setdefault("challenge_counts", {})
        counts[challenge] = counts.get(challenge, 0) + 1

    stats = rec.setdefault("tier_stats", {}).setdefault(tier, {"ok": 0, "miss": 0})
    if outcome == "ok":
        rec["successes"] += 1
        stats["ok"] += 1
        rec["winning_tier"] = tier
        rec["needs_egress"] = False   # host recovered
        rec["egress_failures"] = 0    # reset the escalation counter
        _clear_url_skip(url, db)      # self-heal a previously-excluded URL
    else:
        stats["miss"] += 1

    hosts[host] = rec

    _track_url_failure(url, outcome, db)
    _track_egress(host, tier, outcome, db)
    _track_domain_failure(host, outcome, db)

    _log_event(url, tier, outcome, md_len=md_len, error=error,
               latency_ms=latency_ms, status_code=status_code, challenge=challenge)
