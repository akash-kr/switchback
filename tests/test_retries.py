"""Unit tests for configurable per-tier retries (switchback.orchestrator).

Offline: the cascade is replaced with a fake tier that fails a controlled number
of times. Verifies the retry knobs (SCRAPER_TIER_RETRIES[_<TIER>],
SCRAPER_TIER_RETRY_ON) and — crucially — that intermediate retries are NOT
persisted to the botwall policy DB (so retries can't inflate the self-healing
skip / needs_egress counters).

Run with: pytest tests/test_retries.py
"""
from __future__ import annotations

import time

import pytest

from switchback import orchestrator as O
from switchback.policy.gates import BotWall, RateLimited


class _FakeTier:
    """fetch() raises `exc` for the first `fail_times` calls, then returns content
    (unless fail_times is None → always fail)."""
    def __init__(self, name, *, exc, fail_times=None):
        self.NAME = name
        self.PAID = False
        self._exc = exc
        self._fail_times = fail_times
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        if self._fail_times is None or self.calls <= self._fail_times:
            raise self._exc
        return "REAL ARTICLE CONTENT " * 50


class _Root:
    def set(self, *a, **k):
        pass


@pytest.fixture
def harness(monkeypatch):
    """Install one fake tier + neutralise side-effects, but SPY on botwall.record
    so we can assert exactly which outcomes were persisted to the policy DB."""
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(O.botwall, "record",
                        lambda db, url, tier, outcome, **k: recorded.append((tier, outcome)))
    monkeypatch.setattr(O.botwall, "log_final", lambda *a, **k: None)
    monkeypatch.setattr(O.botwall, "needs_egress", lambda *a, **k: False)
    monkeypatch.setattr(O.botwall, "winning_tier", lambda *a, **k: None)
    monkeypatch.setattr(O.content_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(O.session_cache, "forget", lambda *a, **k: None)

    def install(tier):
        monkeypatch.setattr(O, "TIERS", [tier])
        monkeypatch.setattr(O, "INDEX", {tier.NAME: 0})
    return install, recorded, monkeypatch


def _run(deadline_s=5.0):
    t0 = time.monotonic()
    return O._run_cascade("https://x.example/a", "x.example",
                          {"hosts": {}, "urls": {}}, _Root(), t0, t0 + deadline_s)


def test_transient_retry_then_success(harness):
    install, recorded, mp = harness
    tier = _FakeTier("faketier", exc=RateLimited("429"), fail_times=2)
    install(tier)
    mp.setenv("SCRAPER_TIER_RETRIES_FAKETIER", "2")

    res = _run()

    assert res.ok and res.source_method == "faketier"
    assert tier.calls == 3                                  # 2 retried + 1 ok
    # The two retried misses must NOT hit the policy DB — only the final OK does.
    assert recorded == [("faketier", "ok")]
    assert [a.outcome for a in res.attempts] == ["rate_limited", "rate_limited", "ok"]


def test_default_no_retry(harness):
    install, recorded, mp = harness
    tier = _FakeTier("faketier", exc=RateLimited("429"), fail_times=None)
    install(tier)
    # no retry env → default 0

    res = _run()

    assert not res.ok
    assert tier.calls == 1
    assert recorded == [("faketier", "rate_limited")]      # the single failure persists


def test_non_retryable_outcome_not_retried(harness):
    install, recorded, mp = harness
    tier = _FakeTier("faketier", exc=BotWall("wall", vendor="cloudflare"), fail_times=None)
    install(tier)
    mp.setenv("SCRAPER_TIER_RETRIES_FAKETIER", "2")
    mp.setenv("SCRAPER_TIER_RETRY_ON", "timeout,rate_limited,connection")  # excludes botwall

    res = _run()

    assert not res.ok
    assert tier.calls == 1                                  # botwall not retried by default


def test_botwall_retry_when_opted_in(harness):
    install, recorded, mp = harness
    tier = _FakeTier("faketier", exc=BotWall("wall", vendor="cloudflare"), fail_times=1)
    install(tier)
    mp.setenv("SCRAPER_TIER_RETRIES_FAKETIER", "2")
    mp.setenv("SCRAPER_TIER_RETRY_ON", "botwall,timeout")  # widen for rotating proxy

    res = _run()

    assert res.ok
    assert tier.calls == 2                                  # 1 retried botwall + 1 ok
    assert recorded == [("faketier", "ok")]


def test_deadline_bounds_retries(harness):
    install, recorded, mp = harness

    class _SlowFail(_FakeTier):
        def fetch(self, url):
            self.calls += 1
            time.sleep(0.1)
            raise RateLimited("429")

    tier = _SlowFail("faketier", exc=RateLimited("429"), fail_times=None)
    install(tier)
    mp.setenv("SCRAPER_TIER_RETRIES_FAKETIER", "5")

    res = _run(deadline_s=0.05)                             # expires before any retry

    assert not res.ok
    assert tier.calls == 1                                  # bounded by the deadline


def test_global_default_and_override():
    """The config helpers resolve per-tier override → global default → 0."""
    import os
    for k in list(os.environ):
        if k.startswith("SCRAPER_TIER_RETR"):
            del os.environ[k]
    assert O._retries_for("tier_2") == 0
    os.environ["SCRAPER_TIER_RETRIES"] = "3"
    assert O._retries_for("tier_4") == 3
    os.environ["SCRAPER_TIER_RETRIES_TIER_2"] = "5"
    assert O._retries_for("tier_2") == 5
    del os.environ["SCRAPER_TIER_RETRIES"]
    del os.environ["SCRAPER_TIER_RETRIES_TIER_2"]
