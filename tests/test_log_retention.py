"""SCRAPER_BOTWALL_LOG_RETENTION_DAYS: opt-in trimming of the botwall audit logs.

Unset (0) must leave the logs exactly as before; >0 drops lines older than N days,
keeps recent and unparseable lines and the file mode, and rewrites at most hourly.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from switchback.policy import botwall


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def logs(tmp_path, monkeypatch):
    paths = {name: str(tmp_path / name) for name in
             ("botwall_events.jsonl", "botwall_excluded.jsonl", "botwall_excluded.log")}
    monkeypatch.setattr(botwall, "EVENTS_PATH", paths["botwall_events.jsonl"])
    monkeypatch.setattr(botwall, "EXCLUDED_JSONL", paths["botwall_excluded.jsonl"])
    monkeypatch.setattr(botwall, "EXCLUDED_LOG", paths["botwall_excluded.log"])
    monkeypatch.setattr(botwall, "_last_trim", 0.0)
    old, new = _ts(10), _ts(1)
    seed = {
        "botwall_events.jsonl": [json.dumps({"ts": old, "url": "https://a.example/old"}),
                                 json.dumps({"ts": new, "url": "https://a.example/new"}),
                                 "not json at all"],
        "botwall_excluded.jsonl": [json.dumps({"ts": old, "url": "https://b.example/old"}),
                                   json.dumps({"ts": new, "url": "https://b.example/new"})],
        "botwall_excluded.log": [f"{old}  EXCLUDED  https://b.example/old  [x]",
                                 f"{new}  EXCLUDED  https://b.example/new  [x]",
                                 "garbage line"],
    }
    for name, lines in seed.items():
        with open(paths[name], "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(paths[name], 0o600)
    return paths


def _read(path):
    with open(path) as f:
        return f.read()


def test_default_unset_keeps_everything(logs):
    """The default (0) never trims: old lines survive, only the new one is appended."""
    assert "SCRAPER_BOTWALL_LOG_RETENTION_DAYS" not in os.environ
    assert botwall.LOG_RETENTION_DAYS == 0        # pinned default: off
    before = {p: _read(p) for p in logs.values()}
    botwall._log_event("https://a.example/x", "tier_2", "ok")
    botwall._log_exclusion("https://b.example/x", "botwall")
    for p, text in before.items():
        assert _read(p).startswith(text)          # nothing removed, only appended
        assert "old" in _read(p)


def test_retention_drops_old_keeps_recent_and_unparseable(logs, monkeypatch):
    monkeypatch.setattr(botwall, "LOG_RETENTION_DAYS", 5)
    botwall._log_exclusion("https://b.example/x", "botwall")
    events = _read(logs["botwall_events.jsonl"])
    assert "a.example/old" not in events
    assert "a.example/new" in events and "not json at all" in events
    for name in ("botwall_excluded.jsonl", "botwall_excluded.log"):
        text = _read(logs[name])
        assert "b.example/old" not in text
        assert "b.example/new" in text and "b.example/x" in text
    assert "garbage line" in _read(logs["botwall_excluded.log"])
    for p in logs.values():
        assert os.stat(p).st_mode & 0o777 == 0o600
        assert not [f for f in os.listdir(os.path.dirname(p)) if ".tmp." in f]


def test_retention_trims_at_most_once_an_hour(logs, monkeypatch):
    monkeypatch.setattr(botwall, "LOG_RETENTION_DAYS", 5)
    botwall._log_event("https://a.example/x", "tier_2", "ok")
    assert "a.example/old" not in _read(logs["botwall_events.jsonl"])
    with open(logs["botwall_events.jsonl"], "a") as f:
        f.write(json.dumps({"ts": _ts(10), "url": "https://a.example/late"}) + "\n")
    botwall._log_event("https://a.example/y", "tier_2", "ok")
    assert "a.example/late" in _read(logs["botwall_events.jsonl"])  # no second rewrite
