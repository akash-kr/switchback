"""Shared pytest fixtures for the integration suite (GLD-2).

Two jobs:
1. Load a gitignored ``.env`` (NYT creds, tunables) without adding a hard
   dependency on python-dotenv — a tiny parser handles it.
2. Hand each test an isolated ``SCRAPER_STATE_DIR`` so the session cache the
   tests write never touches a developer's real ``state/`` dir.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Populate os.environ from <repo>/.env (KEY=VALUE lines), without clobbering
    anything already set in the real environment. No-op when .env is absent."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()


def have_nyt_creds() -> bool:
    return bool(os.getenv("NYT_USERNAME") and os.getenv("NYT_PASSWORD"))


# Reusable skip marker: live tests decorate with this so CI (no creds) stays
# green and the suite documents exactly what's gating the real flow.
requires_nyt_creds = pytest.mark.skipif(
    not have_nyt_creds(),
    reason="NYT_USERNAME/NYT_PASSWORD not set — add them to .env to run the live NYT flow",
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point the session cache at a throwaway state dir and reset its in-memory
    singletons, so each test starts from a clean cache and writes nowhere real."""
    monkeypatch.setenv("SCRAPER_STATE_DIR", str(tmp_path))
    import switchback.session_cache as sc

    # session_cache reads _STATE_DIR/CACHE_PATH/_TTL_S at import time, and caches
    # the loaded DB + auth jar + login hook in module globals. Re-import so the
    # new env is picked up, then return the freshly-bound module.
    sc = importlib.reload(sc)
    return sc
