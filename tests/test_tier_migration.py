"""Upgrade safety: state written by 0.1–0.3 users must survive the rename of the
tiers to plain ``tier_1``…``tier_7``. Covers the two back-compat shims:

  1. ``botwall_db.json`` key migration (old ``winning_tier`` / ``tier_stats`` names
     are remapped on load), so a host keeps its learned fast-path.
  2. the pre-rename timeout env vars are still honored when the new var is unset.
"""
import importlib
import os

from switchback.policy import botwall
from switchback.tiers import INDEX


def test_db_migration_remaps_winning_tier_and_stats():
    """A pre-rename host record is rewritten to the new names in place."""
    hosts = {
        "hard.example": {
            "winning_tier": "tier3_browser",
            "tier_stats": {"tier0_apis": {"ok": 1, "miss": 2},
                           "tier4_firecrawl": {"ok": 0, "miss": 5}},
        },
    }
    changed = botwall._migrate_tier_names(hosts)
    assert changed is True
    rec = hosts["hard.example"]
    assert rec["winning_tier"] == "tier_4"                 # tier3_browser → tier_4
    assert set(rec["tier_stats"]) == {"tier_1", "tier_7"}  # keys remapped
    assert rec["tier_stats"]["tier_1"] == {"ok": 1, "miss": 2}  # counts preserved


def test_db_migration_is_idempotent():
    """Re-running on an already-migrated DB is a no-op (won't churn saves)."""
    hosts = {"h": {"winning_tier": "tier_4",
                   "tier_stats": {"tier_1": {"ok": 1, "miss": 0}}}}
    assert botwall._migrate_tier_names(hosts) is False


def test_migrated_winning_tier_still_routes():
    """The migrated name resolves through INDEX, so _start_index sends the host
    straight to its learned tier instead of falling back to 0 (re-probe)."""
    hosts = {"hard.example": {"winning_tier": "tier3_browser", "tier_stats": {}}}
    botwall._migrate_tier_names(hosts)
    assert INDEX[hosts["hard.example"]["winning_tier"]] == 3  # tier_4 is index 3


def test_legacy_timeout_env_var_still_honored():
    """A 0.3.0 user's SCRAPER_CLOUDSCRAPER_TIMEOUT_S keeps working after the rename."""
    for k in ("SCRAPER_TIER_3_TIMEOUT_S", "SCRAPER_CLOUDSCRAPER_TIMEOUT_S"):
        os.environ.pop(k, None)
    os.environ["SCRAPER_CLOUDSCRAPER_TIMEOUT_S"] = "40"
    import switchback.tiers.tier_3 as t3
    try:
        importlib.reload(t3)
        assert t3._TIMEOUT_S == 40.0
    finally:
        del os.environ["SCRAPER_CLOUDSCRAPER_TIMEOUT_S"]
        importlib.reload(t3)  # restore default for other tests


def test_legacy_camoufox_ms_var_converted_to_seconds():
    """The old _MS knob is honored and converted to the new seconds unit."""
    for k in ("SCRAPER_TIER_5_TIMEOUT_S", "SCRAPER_CAMOUFOX_TIMEOUT_MS"):
        os.environ.pop(k, None)
    os.environ["SCRAPER_CAMOUFOX_TIMEOUT_MS"] = "60000"
    import switchback.tiers.tier_5 as t5
    try:
        importlib.reload(t5)
        assert t5._TIMEOUT_S == 60.0
        assert t5._TIMEOUT_MS == 60000
    finally:
        del os.environ["SCRAPER_CAMOUFOX_TIMEOUT_MS"]
        importlib.reload(t5)


def test_new_timeout_env_var_wins_over_legacy():
    """When both are set the new var takes precedence."""
    os.environ["SCRAPER_TIER_3_TIMEOUT_S"] = "99"
    os.environ["SCRAPER_CLOUDSCRAPER_TIMEOUT_S"] = "40"
    import switchback.tiers.tier_3 as t3
    try:
        importlib.reload(t3)
        assert t3._TIMEOUT_S == 99.0
    finally:
        del os.environ["SCRAPER_TIER_3_TIMEOUT_S"]
        del os.environ["SCRAPER_CLOUDSCRAPER_TIMEOUT_S"]
        importlib.reload(t3)
