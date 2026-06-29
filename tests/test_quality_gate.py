"""Unit tests for the content-quality gate (switchback.policy.gates.check).

Pure + offline. Verifies that content which clears the length floor but isn't an
article — an unrendered media placeholder or a mostly-links nav/listing shell —
is rejected as ShortContent, while real articles (including short, link-heavy
ones) still pass. Run with: pytest tests/test_quality_gate.py
"""
from __future__ import annotations

import pytest

from switchback.policy.gates import ShortContent, check

URL = "https://news.example/article"

# A real, short, link-heavy news brief (the kind that must NOT be rejected):
# real prose, but plenty of links and a short longest-paragraph.
REAL_SHORT = (
    "# Vehicle fire on Interstate 10 causes delays\n\n"
    "One person is dead after a three-vehicle crash shut down eastbound "
    "Interstate 10 near Picacho Peak on Saturday afternoon. All eastbound lanes "
    "were closed at milepost 224 while crews cleared the wreckage.\n\n"
    "Road closures will snarl Phoenix-area travel this weekend as work shuts "
    "lanes on five freeways. Here are the detours drivers need to know about.\n\n"
    "[More traffic](https://news.example/traffic) "
    "[Weather](https://news.example/weather) [Home](https://news.example/)\n"
) * 4


def test_nav_shell_rejected():
    """Mostly-links listing with little real text -> ShortContent."""
    links = " ".join(
        f"[Upcoming event number {i} starting soon](https://x.example/e/{i})"
        for i in range(60)
    )
    md = "# Latest Videos\n\n" + links + "\n\nBy Staff\n"
    assert len(md) > 2000  # clears the length floor
    with pytest.raises(ShortContent):
        check(URL, md)


def test_unrendered_video_placeholder_rejected():
    """A media page whose body never rendered ('Loading video…') -> ShortContent,
    even though sidebar headlines push it over the length floor."""
    sidebar = "\n".join(f"Some unrelated headline number {i} about the news today"
                        for i in range(80))
    md = "Loading video...\n\n" + sidebar
    assert len(md) > 2000
    with pytest.raises(ShortContent):
        check(URL, md)


def test_real_short_article_passes():
    """A genuine short, link-heavy news brief must clear the gate unchanged."""
    assert len(REAL_SHORT) > 2000
    assert check(URL, REAL_SHORT) == REAL_SHORT


def test_real_long_article_passes():
    body = ("This is a substantial paragraph of real article prose that conveys "
            "actual information to the reader and contains no links at all. ") * 20
    md = "# A Real Story\n\n" + body
    assert check(URL, md) == md


def test_length_floor_still_applies():
    with pytest.raises(ShortContent):
        check(URL, "too short to be anything")
