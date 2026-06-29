"""Unit tests for the Firecrawl fallback timer (switchback.orchestrator).

Offline: the cascade is replaced with fake tiers, so no network. Verifies that
when the cheap/browser tiers burn the per-URL deadline, the cascade still reaches
the paid last-resort tier instead of quitting with deadline_exceeded — and that
the behaviour is gated on a paid tier actually being available + enabled.

Run with: pytest tests/test_deadline_budget.py
"""
from __future__ import annotations

import time

import pytest

from switchback import orchestrator as O
from switchback.policy.gates import BotWall


class _FakeTier:
    def __init__(self, name, *, paid=False, sleep=0.0, win=False, disabled=False):
        self.NAME = name
        self.PAID = paid
        self._sleep = sleep
        self._win = win
        self._disabled = disabled
        self.calls = []

    def disabled(self):
        return self._disabled

    def fetch(self, url):
        self.calls.append(url)
        if self._sleep:
            time.sleep(self._sleep)
        if self._win:
            return "REAL ARTICLE CONTENT " * 200   # clears the length gate
        raise BotWall("wall", vendor="cloudflare")


class _Root:
    def set(self, *a, **k):
        pass


@pytest.fixture
def stub_cascade(monkeypatch):
    """Neutralise persistence/tracing side-effects so the test is pure."""
    monkeypatch.setattr(O.botwall, "record", lambda *a, **k: None)
    monkeypatch.setattr(O.botwall, "log_final", lambda *a, **k: None)
    monkeypatch.setattr(O.botwall, "needs_egress", lambda *a, **k: False)
    monkeypatch.setattr(O.botwall, "winning_tier", lambda *a, **k: None)
    monkeypatch.setattr(O.content_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(O.session_cache, "forget", lambda *a, **k: None)

    def _install(tiers, fallback_after):
        monkeypatch.setattr(O, "TIERS", tiers)
        monkeypatch.setattr(O, "INDEX", {t.NAME: i for i, t in enumerate(tiers)})
        monkeypatch.setattr(O, "_FIRECRAWL_FALLBACK_AFTER_S", fallback_after)
    return _install


def _run(tiers, *, deadline_s=0.5):
    t0 = time.monotonic()
    return O._run_cascade("https://hard.example/article", "hard.example",
                          {"hosts": {}, "urls": {}}, _Root(), t0, t0 + deadline_s)


def test_reaches_paid_tier_after_budget_burned(stub_cascade):
    """A slow cheap tier blows the deadline, but the paid tier still runs."""
    a = _FakeTier("t_a", sleep=0.6)              # overruns the 0.5s deadline
    b = _FakeTier("t_b", sleep=0.6)              # must be skipped, not run
    paid = _FakeTier("t_paid", paid=True, win=True)
    stub_cascade([a, b, paid], fallback_after=0.3)

    res = _run([a, b, paid])

    assert res.ok and res.source_method == "t_paid"
    assert b.calls == []                          # skipped to fall back to Firecrawl
    assert any(att.tier == "t_b" and att.outcome == "skipped_for_budget"
               for att in res.attempts)


def test_no_paid_tier_means_normal_deadline(stub_cascade):
    """With the paid tier disabled there's nothing to fall back to, so the cascade
    ends in deadline_exceeded (unchanged behaviour)."""
    a = _FakeTier("t_a", sleep=0.6)
    b = _FakeTier("t_b", sleep=0.6)
    paid = _FakeTier("t_paid", paid=True, win=True, disabled=True)
    stub_cascade([a, b, paid], fallback_after=0.3)

    res = _run([a, b, paid])

    assert not res.ok
    assert res.final_outcome == "deadline_exceeded"


def test_fallback_disabled_keeps_old_behaviour(stub_cascade):
    """Fallback disabled (0) reproduces the bug: the paid tier is starved by the
    deadline — proving the early fallback is what rescues it above."""
    a = _FakeTier("t_a", sleep=0.6)
    b = _FakeTier("t_b", sleep=0.6)
    paid = _FakeTier("t_paid", paid=True, win=True)
    stub_cascade([a, b, paid], fallback_after=0.0)

    res = _run([a, b, paid])

    assert not res.ok and res.final_outcome == "deadline_exceeded"
    assert paid.calls == []                       # never reached without the fallback
