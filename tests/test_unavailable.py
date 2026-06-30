"""Unit tests for the `unavailable` outcome — a tier whose optional dependency is
missing/old/not-installed-yet must surface distinctly, not as a generic error and
not masked by an earlier site bot-wall.

Pure + offline: fake tiers, botwall/content_cache stubbed so nothing touches real
state. Run with: pytest tests/test_unavailable.py
"""
from __future__ import annotations

import types

import pytest

import switchback.orchestrator as orch
from switchback.policy.gates import BotWall, Unavailable


def _tier(name, fetch):
    m = types.SimpleNamespace(NAME=name, PAID=False, fetch=fetch)
    return m


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Stub policy + cache so the cascade runs without touching real state."""
    monkeypatch.setattr(orch.botwall, "load_db", lambda: {})
    monkeypatch.setattr(orch.botwall, "save_db", lambda db: None)
    monkeypatch.setattr(orch.botwall, "is_skipped", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "is_url_skipped", lambda u, db: False)
    monkeypatch.setattr(orch.botwall, "needs_egress", lambda h, db: False)
    monkeypatch.setattr(orch.botwall, "winning_tier", lambda h, db: None)
    monkeypatch.setattr(orch.botwall, "log_final", lambda *a, **k: None)
    monkeypatch.setattr(orch.botwall, "record", lambda *a, **k: None)
    monkeypatch.setattr(orch.content_cache, "get", lambda u, fmt: None)
    monkeypatch.setattr(orch.content_cache, "put", lambda *a, **k: None)
    orch._unavail_warned.clear()


def _run(tiers):
    orch.TIERS = tiers
    orch.INDEX = {t.NAME: i for i, t in enumerate(tiers)}
    return orch.run_detailed(["https://example.test/x"])[0]


def test_unavailable_outcome_is_distinct():
    def boom(url):
        raise Unavailable("cloudscraper not installed — pip install ...")
    out = _run([_tier("tier_3", boom)])
    assert out.ok is False
    assert out.error_class == "unavailable"
    assert [a.outcome for a in out.attempts] == ["unavailable"]
    assert "pip install" in out.attempts[0].error


def test_unavailable_outranks_botwall_masking():
    """A real site wall (tier1) plus an unavailable capable tier must report the
    fixable environment problem, not 'botwall'."""
    def wall(url):
        raise BotWall("cloudflare", vendor="cloudflare")
    def missing(url):
        raise Unavailable("patchright Chromium not installed — patchright install chromium")
    out = _run([_tier("tier_2", wall),
                _tier("tier_4", missing)])
    assert out.error_class == "unavailable"
    outcomes = [a.outcome for a in out.attempts]
    assert outcomes == ["botwall", "unavailable"]


def test_healthy_tier_is_unaffected():
    """A tier that returns content still wins — no behavior change."""
    out = _run([_tier("tier_2", lambda url: "# ok\n\nbody")])
    assert out.ok is True
    assert out.source_method == "tier_2"
    assert out.error_class == ""


def test_dominant_failure_ranks_unavailable_top():
    A = orch.TierAttempt
    attempts = [A("t1", "botwall"), A("t2", "unavailable"), A("t3", "error")]
    assert orch._dominant_failure(attempts)[0] == "unavailable"
