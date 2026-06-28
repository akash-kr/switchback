"""Deterministic offline login hook for the machinery tests — never hits the
network. Returns a fixed session cookie so refresh_login()'s persist path can be
asserted without real credentials. The live flow uses tests.nyt_login instead."""
from __future__ import annotations


def login(host: str) -> dict:
    return {"NYT-S": "fake-session", "NYT-MPS": "fake-mps"}
