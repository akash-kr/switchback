"""Unit tests for the content-quality gate (switchback.policy.gates.check).

Pure + offline. Verifies that content which clears the length floor but isn't an
article — an unrendered media placeholder or a mostly-links nav/listing shell —
is rejected as ShortContent, while real articles (including short, link-heavy
ones) still pass. Run with: pytest tests/test_quality_gate.py
"""
from __future__ import annotations

import pytest

from switchback.policy.gates import BotWall, ShortContent, check

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


# --- Inline data-URI images must not hide a block page or pad the length floor.
# Regression: Indeed's Cloudflare block page renders two base64 SVG logos
# (~7.2k chars) *before* "You have been blocked". The 600-char head scan never
# saw the phrase and the logos cleared the 2000-char floor, so the block page was
# returned as a tier_4 "OK" success. Shape mirrors the real page (logo sizes
# 6644 / 540 chars); IP and Ray ID are placeholders.
def _data_uri(n: int) -> str:
    return "data:image/svg+xml;base64," + ("PHN2Zy" * n)[: n - 26]


INDEED_BLOCK = (
    "Blocked - Indeed.com\n"
    f"![]({_data_uri(6644)})\n"
    f"![]({_data_uri(540)})\n\n"
    "# Request Blocked\n\n"
    "You have been blocked. If you believe this in error, please go to "
    "support.indeed.com and reference the following information:\n"
    "Your Ray ID for this request is 0123456789abcdef\n"
    "Your current IP for this request is 203.0.113.7\n\n"
    "[Return home →](https://www.indeed.com/)\n\n"
    "[Troubleshooting Cloudflare Errors](https://www.indeed.com/help/cloudflare-errors)\n\n"
    "Need more help?\n[Contact us](https://www.indeed.com/support/contact)"
)


def test_block_page_behind_inline_images_is_botwall():
    """The Indeed repro: clears the length floor only because of the logos, and
    the block phrase sits past the head window. Must be a BotWall, not a success."""
    assert len(INDEED_BLOCK) > 2000
    assert INDEED_BLOCK.lower().find("you have been blocked") > 600
    with pytest.raises(BotWall) as ei:
        check("https://www.indeed.com/career-advice/x", INDEED_BLOCK)
    assert ei.value.vendor == "cloudflare"


def test_cloudflare_waf_block_page_is_botwall():
    """Cloudflare's stock WAF (1020) block page copy is a wall too."""
    md = ("Attention Required\n\n# Sorry, you have been blocked\n\n"
          "You are unable to access example.com\n\n") + ("filler text " * 200)
    with pytest.raises(BotWall):
        check(URL, md)


def test_inline_images_do_not_count_toward_length_floor():
    """A thin page padded past 2000 chars by a data-URI image is still short."""
    md = f"# Title\n\n![]({_data_uri(5000)})\n\nA couple of real sentences only."
    assert len(md) > 2000
    with pytest.raises(ShortContent):
        check(URL, md)


def test_real_article_with_inline_images_passes_unchanged():
    """A real article that embeds data-URI images still clears the gate, and the
    returned markdown is byte-for-byte what came in (images kept)."""
    md = f"# A Real Story\n\n![chart]({_data_uri(4000)})\n\n" + REAL_SHORT
    assert check(URL, md) == md


def test_block_phrase_deep_in_article_body_passes():
    """Head-only scan is preserved: an article that merely *mentions* the phrase
    far down its body is not a wall."""
    md = REAL_SHORT + "\nOne reader wrote in: 'you have been blocked' was all I saw.\n"
    assert check(URL, md) == md
