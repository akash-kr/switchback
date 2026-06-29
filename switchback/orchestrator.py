"""Cascade runner: route → run tiers in cost order → stop at first success.

One trace per URL, one span per tier attempt. Botwall governs skip-listing and
winning-tier routing; every outcome is recorded so the policy self-heals.

Failures are first-class: every attempt is classified (see gates.classify_error)
so a hard 403/429 escalates egress, the per-tier reasons are returned to callers
via `run_detailed()`/`ScrapeOutcome`, and one aggregate event is logged + traced
per URL. `run()` stays successes-only for backward compatibility.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field

from . import content_cache, egress, session_cache
from .normalize import active_format, output_format_scope
from .policy import botwall
from .policy.gates import (BotWall, RateLimited, ShortContent, Unavailable,
                           classify_error, host_of)
from .tiers import TIERS, INDEX
from .tracing import Attr, flush, span

logger = logging.getLogger(__name__)

# Per-request wall-clock budget. Checked between tiers so a single URL can't run
# the whole cascade of timeouts; overridable via env. 45s balances latency vs
# coverage: roughly fits a Camoufox solve (~40s) that starts after the cheaper
# tiers fail fast, while still bounding the worst case.
_DEADLINE_S = float(os.getenv("SCRAPER_DEADLINE_S", "45"))

# Fall back to Firecrawl after this many seconds on a URL. On a hard host the
# cheaper tiers can burn the whole deadline (e.g. cloudscraper's ~25s timeout +
# two browser solves), so the cascade would hit the deadline and quit *before*
# ever trying the one tier that reliably works. Once this much time has elapsed,
# we stop starting more local tiers and jump straight to Firecrawl — so the
# safety net actually gets a turn. Default 25s leaves ~20s of the 45s deadline
# for Firecrawl. Only applies when a paid, enabled tier is still ahead; 0 = off.
_FIRECRAWL_FALLBACK_AFTER_S = float(os.getenv("SCRAPER_FIRECRAWL_FALLBACK_AFTER_S", "25"))

# Exponential backoff between tiers after a *transient* failure (rate_limited /
# timeout) — gives a rate limiter or a slow origin a moment before the next tier
# hammers it. Disabled by default (base 0) so behaviour is unchanged until opted
# in. delay = min(MAX, BASE·2^(n−1)) with 50–100% jitter; never sleeps past the
# per-request deadline.
_BACKOFF_BASE_MS = float(os.getenv("SCRAPER_BACKOFF_BASE_MS", "0"))
_BACKOFF_MAX_MS = float(os.getenv("SCRAPER_BACKOFF_MAX_MS", "8000"))
_TRANSIENT = ("rate_limited", "timeout")


def _maybe_backoff(transient_n: int, deadline: float) -> None:
    if not _BACKOFF_BASE_MS or transient_n <= 0:
        return
    delay = min(_BACKOFF_MAX_MS, _BACKOFF_BASE_MS * (2 ** (transient_n - 1)))
    delay = delay * (0.5 + random.random() * 0.5) / 1000.0  # jitter → seconds
    if time.monotonic() + delay >= deadline:  # don't burn the whole budget sleeping
        return
    time.sleep(delay)

# Per-attempt outcomes that aren't real failures (don't carry a failure reason).
_NON_FAILURE = ("ok", "not_applicable", "disabled", "skipped_for_budget")

# How explanatory each failure class is, for picking the reason that best
# describes why a URL failed. A real wall (403 / bot-wall) outranks a trailing
# config error (e.g. Firecrawl with no API key → "error"), so the verdict points
# at the actual blocker rather than the last thing that happened to throw.
_FAILURE_PRIORITY = {
    # A missing/old/not-yet-installed tier dependency is an operator-fixable
    # environment problem; rank it above site walls so it surfaces as the verdict
    # instead of being masked as "botwall" when the capable tiers can't run.
    "unavailable": 6,
    "botwall": 5, "http_block": 5,
    "rate_limited": 4, "short_content": 4,
    "timeout": 3, "connection": 3,
    "http_error": 2,
    "error": 1,
}

# Tiers whose dependency we've already warned about this process — the install
# hint is logged once at WARNING, not per-URL across a whole batch.
_unavail_warned: set[str] = set()


@dataclass
class ScrapeResult:
    url: str
    markdown: str       # the rendered content (format named by `format`)
    source_method: str  # tier NAME that won
    format: str = "markdown"  # markdown | markdown_trimmed | html | html_selectors


@dataclass
class TierAttempt:
    """One tier's attempt on a URL — what it was and why it ended."""
    tier: str
    outcome: str                  # ok | botwall | short_content | http_block | …
    error: str = ""
    status_code: int | None = None
    latency_ms: int | None = None


@dataclass
class ScrapeOutcome:
    """Full per-URL result, success or failure, with the cascade it took."""
    url: str
    ok: bool
    markdown: str = ""
    source_method: str = ""        # winning tier (on success)
    final_outcome: str = ""        # ok | all_failed | deadline_exceeded | *_skipped
    error_class: str = ""          # dominant failure class (on failure)
    status_code: int | None = None
    latency_ms: int | None = None
    egress: str = "direct"         # "egress" if routed via SCRAPER_EGRESS_PROXY, else "direct"
    wire_bytes: int = 0            # bytes transferred over the network (cost basis for proxy GB)
    format: str = "markdown"       # output format of `markdown` (the content field)
    attempts: list[TierAttempt] = field(default_factory=list)


def _dominant_failure(attempts: list[TierAttempt]) -> tuple[str, int | None]:
    """The failure that best explains 'why this URL failed': the highest-priority
    real failing attempt (ties resolve to the later, more capable tier)."""
    best: tuple[int, str, int | None] | None = None
    for a in attempts:
        if a.outcome in _NON_FAILURE:
            continue
        pr = _FAILURE_PRIORITY.get(a.outcome, 1)
        if best is None or pr >= best[0]:
            best = (pr, a.outcome, a.status_code)
    return (best[1], best[2]) if best else ("", None)


def _start_index(url: str, db: dict) -> int:
    """Begin at the host's known-good rung (fall through on regression).

    For a host the local tiers keep walling (needs_egress) we escalate egress,
    cheapest first:
      1. If a residential proxy is wired (SCRAPER_EGRESS_PROXY), rerun from the
         top — the HTTP tiers now go through it (~0.2MB) instead of jumping
         straight to a multi-MB remote browser render.
      2. Else, if the residential CDP browser is enabled, jump to it.
      3. Else fall back to normal routing (don't strand the host past every
         usable tier)."""
    host = host_of(url)
    if botwall.needs_egress(host, db):
        if egress.has_egress_proxy():
            return 0
        res_i = INDEX.get("tier_residential")
        if res_i is not None:
            disabled_fn = getattr(TIERS[res_i], "disabled", None)
            if not (disabled_fn and disabled_fn()):
                return res_i
    wt = botwall.winning_tier(host, db)
    return INDEX.get(wt, 0) if wt else 0


def _record_failure(sp, attempts, db, url, tier_name, outcome, exc, status, dt,
                    challenge=None):
    """Annotate the span, persist to botwall, and append the attempt — for one
    failed tier attempt. Shared by every except branch so classification,
    tracing, and the event log never drift apart. `challenge` names the bot-wall
    vendor when one was served, so the policy can learn it per host."""
    msg = f"{type(exc).__name__}: {exc}"
    sp.set(Attr.OUTCOME, outcome)
    sp.set(Attr.ERROR, msg)
    sp.set(Attr.ERROR_CLASS, outcome)
    sp.set(Attr.CHALLENGE, challenge)
    sp.set(Attr.STATUS_CODE, status)
    sp.set(Attr.LATENCY_MS, dt)
    botwall.record(db, url, tier_name, outcome, error=msg, latency_ms=dt,
                   status_code=status, challenge=challenge)
    # A wall on a host we had a cached cf_clearance for means the cookie is stale
    # or IP-mismatched: drop it so the next attempt re-solves instead of replaying.
    if outcome in ("botwall", "http_block"):
        session_cache.forget(url)
    attempts.append(TierAttempt(tier_name, outcome, msg, status, dt))
    log = logger.info if outcome in ("botwall", "short_content") else logger.warning
    log(f"{tier_name} {outcome} {url}"
        + (f" [{status}]" if status else "") + f": {exc}")


def _skipped(url, root, outcome, reason) -> ScrapeOutcome:
    """Terminal short-circuit (domain/url skip): trace + aggregate event."""
    logger.info(f"{outcome}: {url} [{reason}]")
    root.set(Attr.OUTCOME, outcome)
    botwall.log_final(url, outcome, error=reason)
    return ScrapeOutcome(url, False, final_outcome=outcome, error_class=outcome)


def _run_one(url: str, db: dict) -> ScrapeOutcome:
    host = host_of(url)
    t0 = time.monotonic()
    deadline = t0 + _DEADLINE_S
    egress.take_wire_bytes()  # zero the per-thread wire-byte tally for this URL
    with span("scrape", **{Attr.HOST: host, Attr.DEADLINE_S: _DEADLINE_S}) as root:
        if botwall.is_skipped(host, db):
            return _skipped(url, root, "domain_skipped",
                            db["hosts"][host].get("reason", ""))
        if botwall.is_url_skipped(url, db):
            return _skipped(url, root, "url_excluded",
                            db.get("urls", {}).get(url, {}).get("reason", ""))
        hit = content_cache.get(url, active_format())
        if hit:
            md, method = hit
            root.set(Attr.OUTCOME, "cache_hit")
            root.set(Attr.SOURCE, method)
            root.set(Attr.MD_LEN, len(md))
            logger.info(f"cache_hit {url} (was {method})")
            return ScrapeOutcome(url, True, markdown=md, source_method=method,
                                 final_outcome="ok", format=active_format())
        # A needs_egress host runs the whole cascade in the egress scope, so the
        # tiers route through SCRAPER_EGRESS_PROXY (when set); easy hosts stay
        # direct and never spend residential bandwidth.
        with egress.egress_scope(botwall.needs_egress(host, db)):
            res = _run_cascade(url, host, db, root, t0, deadline)
            # Dead logged-in session? For an authed host with a login hook wired,
            # refresh the cookies once and re-run on a fresh budget — the
            # refreshed cookies overlay every tier (and persist for later runs).
            if (not res.ok and res.error_class in ("botwall", "http_block")
                    and session_cache.has_login_hook()
                    and session_cache.is_authed_host(url)
                    and session_cache.refresh_login(url)):
                logger.info(f"re-running after login refresh: {url}")
                rt = time.monotonic()
                res = _run_cascade(url, host, db, root, rt, rt + _DEADLINE_S)
            # Record which egress this URL's cascade ran on, while still in scope —
            # "egress" means its bytes were metered residential-proxy bandwidth.
            res.egress = egress.scope_label()
            res.wire_bytes = egress.take_wire_bytes()
            return res


def _enabled_paid_ahead(i: int) -> bool:
    """Is there a paid, currently-enabled tier after index i? (i.e. a last-resort
    worth reserving budget for.)"""
    for tier in TIERS[i + 1:]:
        if getattr(tier, "PAID", False):
            disabled_fn = getattr(tier, "disabled", None)
            if not (disabled_fn and disabled_fn()):
                return True
    return False


def _run_cascade(url, host, db, root, t0, deadline) -> ScrapeOutcome:
    attempts: list[TierAttempt] = []
    transient = 0  # count of rate_limited/timeout misses so far (drives backoff)
    start = _start_index(url, db)
    for i, tier in enumerate(TIERS):
        if i < start:
            continue

        # Env/feature gate (e.g. paid Firecrawl off, residential not wired).
        disabled_fn = getattr(tier, "disabled", None)
        if disabled_fn and disabled_fn():
            logger.info(f"{tier.NAME} disabled; skipping {url}")
            attempts.append(TierAttempt(tier.NAME, "disabled"))
            continue

        # Short-circuit: cloudscraper solves Cloudflare challenges, not IP-reputation
        # blocks. If a cheaper HTTP tier already hit a hard http_block (bare 403/401),
        # the same datacenter IP will block cloudscraper too — skip its (up to ~25s)
        # solve attempt and go straight to the browser/egress tiers. A real CF
        # challenge surfaces as `botwall`, not `http_block`, so this never skips a
        # host cloudscraper could actually clear.
        if tier.NAME == "tier2_cloudscraper" and any(
                a.outcome == "http_block" for a in attempts):
            logger.info(f"{tier.NAME} skipped (prior hard IP block): {url}")
            attempts.append(TierAttempt(tier.NAME, "not_applicable"))
            continue

        # Fall back to Firecrawl: once enough time has elapsed on this URL and a
        # paid enabled tier is still ahead, skip this (non-paid) tier and any
        # others so the paid tier actually gets a turn instead of the cascade
        # dying on the deadline mid-browser-solve.
        if (_FIRECRAWL_FALLBACK_AFTER_S and not getattr(tier, "PAID", False)
                and (time.monotonic() - t0) >= _FIRECRAWL_FALLBACK_AFTER_S
                and _enabled_paid_ahead(i)):
            logger.info(
                f"{tier.NAME} skipped after {_FIRECRAWL_FALLBACK_AFTER_S}s to "
                f"fall back to Firecrawl (last resort): {url}")
            attempts.append(TierAttempt(tier.NAME, "skipped_for_budget"))
            continue

        # Limit: stop before starting another tier if we're out of budget. The
        # paid last resort is exempt — if the cascade reached it, let it run even
        # a touch over the deadline rather than quit with nothing (it has its own
        # internal timeout). Non-paid tiers with a paid tier ahead were already
        # skipped above, so this only ever quits when no paid tier remains.
        if time.monotonic() >= deadline and not getattr(tier, "PAID", False):
            total = int((time.monotonic() - t0) * 1000)
            ec, sc = _dominant_failure(attempts)
            root.set(Attr.OUTCOME, "deadline_exceeded")
            root.set(Attr.ERROR_CLASS, ec or "deadline_exceeded")
            root.set(Attr.STATUS_CODE, sc)
            root.set(Attr.LATENCY_MS, total)
            logger.warning(
                f"deadline {_DEADLINE_S}s exceeded before {tier.NAME} "
                f"({total}ms): {url}")
            botwall.log_final(url, "deadline_exceeded", latency_ms=total,
                              error=ec, status_code=sc)
            return ScrapeOutcome(url, False, final_outcome="deadline_exceeded",
                                 error_class=ec or "deadline_exceeded",
                                 status_code=sc, latency_ms=total, attempts=attempts)

        paid = getattr(tier, "PAID", False)
        with span(tier.NAME, **{Attr.HOST: host, Attr.TIER: tier.NAME}) as sp:
            if paid:
                # Count every invocation so the host can be promoted to skip.
                botwall.record(db, url, tier.NAME, "firecrawl_used")
            ts = time.monotonic()
            try:
                md = tier.fetch(url)
            except BotWall as e:
                dt = int((time.monotonic() - ts) * 1000)
                _record_failure(sp, attempts, db, url, tier.NAME, "botwall", e, None, dt,
                                challenge=getattr(e, "vendor", None))
                continue
            except ShortContent as e:
                dt = int((time.monotonic() - ts) * 1000)
                _record_failure(sp, attempts, db, url, tier.NAME, "short_content", e, None, dt)
                continue
            except RateLimited as e:
                dt = int((time.monotonic() - ts) * 1000)
                _record_failure(sp, attempts, db, url, tier.NAME, "rate_limited", e, 429, dt)
                transient += 1
                _maybe_backoff(transient, deadline)
                continue
            except Unavailable as e:
                # Tier dependency missing/old/not-installed-yet. An environment
                # problem, not a host trait — record the attempt + trace, but
                # don't teach botwall anything about this host, and warn once per
                # tier with the exact fix instead of spamming every URL.
                dt = int((time.monotonic() - ts) * 1000)
                sp.set(Attr.OUTCOME, "unavailable")
                sp.set(Attr.ERROR, str(e))
                sp.set(Attr.ERROR_CLASS, "unavailable")
                sp.set(Attr.LATENCY_MS, dt)
                attempts.append(TierAttempt(tier.NAME, "unavailable", str(e), None, dt))
                if tier.NAME not in _unavail_warned:
                    _unavail_warned.add(tier.NAME)
                    logger.warning(f"{tier.NAME} unavailable: {e}")
                continue
            except Exception as e:
                dt = int((time.monotonic() - ts) * 1000)
                error_class, status = classify_error(e)
                _record_failure(sp, attempts, db, url, tier.NAME, error_class, e, status, dt)
                if error_class in _TRANSIENT:
                    transient += 1
                    _maybe_backoff(transient, deadline)
                continue

            dt = int((time.monotonic() - ts) * 1000)
            if md is None:  # tier not applicable (e.g. no API mirror)
                sp.set(Attr.OUTCOME, "not_applicable")
                sp.set(Attr.LATENCY_MS, dt)
                attempts.append(TierAttempt(tier.NAME, "not_applicable", latency_ms=dt))
                continue

            total = int((time.monotonic() - t0) * 1000)
            sp.set(Attr.OUTCOME, "ok")
            sp.set(Attr.MD_LEN, len(md))
            sp.set(Attr.SOURCE, tier.NAME)
            sp.set(Attr.LATENCY_MS, dt)
            botwall.record(db, url, tier.NAME, "ok", md_len=len(md), latency_ms=dt)
            content_cache.put(url, md, tier.NAME, active_format())
            root.set(Attr.OUTCOME, "ok")
            root.set(Attr.SOURCE, tier.NAME)
            root.set(Attr.LATENCY_MS, total)
            attempts.append(TierAttempt(tier.NAME, "ok", latency_ms=dt))
            logger.info(
                f"{tier.NAME} OK {url} md_len={len(md)} {dt}ms (total {total}ms)")
            return ScrapeOutcome(url, True, markdown=md, source_method=tier.NAME,
                                 final_outcome="ok", latency_ms=total,
                                 format=active_format(), attempts=attempts)

    total = int((time.monotonic() - t0) * 1000)
    ec, sc = _dominant_failure(attempts)
    root.set(Attr.OUTCOME, "all_failed")
    root.set(Attr.ERROR_CLASS, ec or "all_failed")
    root.set(Attr.STATUS_CODE, sc)
    root.set(Attr.LATENCY_MS, total)
    botwall.log_final(url, "all_failed", latency_ms=total, error=ec, status_code=sc)
    logger.warning(f"all tiers failed ({total}ms, {ec or 'no-attempt'}): {url}")
    return ScrapeOutcome(url, False, final_outcome="all_failed",
                         error_class=ec or "all_failed", status_code=sc,
                         latency_ms=total, attempts=attempts)


def run_detailed(urls: list[str], fmt: str | None = None) -> list[ScrapeOutcome]:
    """Scrape each URL; return a full ScrapeOutcome (success or failure with the
    per-tier cascade and a classified reason) for every URL.

    fmt overrides SCRAPER_OUTPUT_FORMAT for this call (None = use the default)."""
    db = botwall.load_db()
    out = []
    try:
        with output_format_scope(fmt):
            for url in urls:
                out.append(_run_one(url, db))
    finally:
        botwall.save_db(db)
        flush()
    return out


def run(urls: list[str], fmt: str | None = None) -> list[ScrapeResult]:
    """Successes only (backward-compatible). Use run_detailed() for failures."""
    return [ScrapeResult(o.url, o.markdown, o.source_method, o.format)
            for o in run_detailed(urls, fmt) if o.ok]
