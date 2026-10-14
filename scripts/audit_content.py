#!/usr/bin/env python3
"""Audit cascade *successes* for false positives — a tier returned content that
cleared the length gate but is actually garbage (a cookie/consent wall, a
paywall/login stub, a bot interstitial, or a nav-only shell with no article).

Pairs with diagnose_urls.py --dump-dir: that saves each success's content; this
scans those files plus the JSON records and flags the suspicious ones.

  python scripts/diagnose_urls.py urls.txt --enable-firecrawl \
         --json diag.json --dump-dir dump/
  python scripts/audit_content.py diag.json dump/

A flag means "a human should eyeball this", not "definitely garbage". The point
is to catch the cases where ok=True but the content isn't the article.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

# Phrases that, appearing anywhere in supposedly-good content, mean we captured a
# wall/stub instead of the article. Lowercased substring match over the body.
WALL_PHRASES = {
    "bot/js wall": [
        "enable javascript", "javascript is disabled", "enable cookies",
        "just a moment", "checking your browser", "verify you are human",
        "verifying you are human", "are you a robot", "press & hold",
        "pardon our interruption", "access denied", "reference #", "ray id",
        "ddos protection", "verifying your device", "verification failed",
    ],
    "consent wall": [
        "we use cookies", "accept all cookies", "manage your cookie",
        "cookie preferences", "we and our partners", "your privacy choices",
        "consent to the use of cookies", "we value your privacy",
    ],
    "paywall/login": [
        "subscribe to continue", "subscribe now to", "create a free account",
        "this content is for subscribers", "sign in to continue",
        "to continue reading", "become a member", "register to read",
        "already a subscriber",
    ],
    "error/missing": [
        "404 not found", "page not found", "page you requested",
        "403 forbidden", "the page you are looking for", "this page isn't available",
    ],
}


def link_density(md: str) -> float:
    """Fraction of 'words' that are inside markdown links — a nav/listing shell
    is almost all links; an article is mostly prose."""
    words = md.split()
    if not words:
        return 1.0
    link_words = sum(len(m.split()) for m in re.findall(r"\[([^\]]+)\]\(", md))
    return link_words / max(len(words), 1)


def longest_paragraph(md: str) -> int:
    """Chars in the longest non-link text block — a real article has at least one
    substantial paragraph; a shell of headings/links does not."""
    best = 0
    for block in re.split(r"\n\s*\n", md):
        text = re.sub(r"\[[^\]]*\]\([^)]*\)", "", block)   # strip md links
        text = re.sub(r"[#*>`\-|]", "", text).strip()
        best = max(best, len(text))
    return best


def audit_one(md: str) -> list[str]:
    """Flag content that is probably NOT the article. Size-aware so a long, real
    article with a cookie/subscribe line in its footer is left alone; the target
    is short stubs and pages whose wall/nav IS the content."""
    flags = []
    low = md.lower()
    head = low[:400]
    lp = longest_paragraph(md)
    ld = link_density(md)
    has_real_prose = lp >= 300            # at least one substantial paragraph

    # bot / error markers: a real article never *opens* with these. Flag if in the
    # head, or anywhere when there's no real prose to anchor it as an article.
    for label in ("bot/js wall", "error/missing"):
        hit = next((p for p in WALL_PHRASES[label] if p in head
                    or (p in low and not has_real_prose)), None)
        if hit:
            flags.append(f"{label}:'{hit}'")

    # consent / paywall: common as boilerplate, so only suspicious when the page
    # is thin (near the gate) or has no real paragraph — i.e. the stub IS the page.
    if not has_real_prose or len(md) < 2600:
        for label in ("consent wall", "paywall/login"):
            hit = next((p for p in WALL_PHRASES[label] if p in low), None)
            if hit:
                flags.append(f"{label}:'{hit}'")

    # structural: a nav/listing shell is mostly links and has no paragraph.
    if ld > 0.6 and not has_real_prose:
        flags.append(f"nav_shell link_density={ld:.2f}")
    if lp < 150:
        flags.append(f"no_prose max_paragraph={lp}c")
    return flags


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit("usage: audit_content.py <records.json> <dump_dir>")
    recs = json.load(open(sys.argv[1]))
    dump = pathlib.Path(sys.argv[2])
    by_n = {r["n"]: r for r in recs}

    audited = clean = flagged = 0
    rows = []
    for r in sorted(recs, key=lambda x: x["n"]):
        if not r["ok"]:
            continue
        matches = list(dump.glob(f"{r['n']:03d}_*"))
        if not matches:
            continue
        md = matches[0].read_text()
        audited += 1
        flags = audit_one(md)
        if flags:
            flagged += 1
            rows.append((r, flags, md))
        else:
            clean += 1

    print(f"audited {audited} successful results: {clean} clean, {flagged} FLAGGED\n")
    print("=" * 78)
    for r, flags, md in sorted(rows, key=lambda x: x[0]["md_len"]):
        print(f"\n⚠  {r['won_tier']:16s} md={r['md_len']:6d}  {r['url']}")
        print(f"   flags: {', '.join(flags)}")
        head = re.sub(r"\s+", " ", md[:240]).strip()
        print(f"   head:  {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
