"""State JSON — the structured data a page ships for its own frontend.

Many sites render from a JSON blob embedded in the HTML: Next.js `__NEXT_DATA__`,
Nuxt, Apollo / Redux initial state, schema.org JSON-LD. It is often cleaner than
the rendered text (prices, ids, specs, dates) and it is already in the HTML we
fetched, so pulling it out costs nothing extra.

    extract_state(html) -> {"json_ld": [...], "__NEXT_DATA__": {...}, ...}

Only valid JSON is returned; a blob that is a JavaScript literal rather than JSON
(e.g. contains `undefined`) is skipped, never guessed at. {} when nothing is found.

Not covered yet: the Next.js App Router flight payload (`self.__next_f.push`),
which is a streamed RSC format rather than JSON.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# `window.__INITIAL_STATE__ = {...}`, `self.__APOLLO_STATE__=`, `window["__X__"] =`,
# `window.__remixContext =` — a global assigned a JSON object/array literal, or
# JSON.parse("...") of an escaped string.
_GLOBAL_RE = re.compile(
    r"""(?:window|self|globalThis)\s*(?:\.\s*(__[A-Za-z0-9_]+)|\[\s*["'](__[A-Za-z0-9_]+)["']\s*\])\s*=\s*""")
_JSON_PARSE_RE = re.compile(r"JSON\.parse\(\s*")

_SCRIPT_JS_TYPES = ("", "text/javascript", "application/javascript", "module")

# Bundler bookkeeping, not page data (e.g. BBC's __LOADABLE_REQUIRED_CHUNKS__ is a
# list of webpack chunk ids).
_NOISE_RE = re.compile(r"LOADABLE|CHUNK|WEBPACK|BUILD_MANIFEST|SSG_MANIFEST", re.I)


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _globals_in(js: str) -> dict:
    """JSON values assigned to `__name` globals in one script body."""
    found: dict = {}
    dec = json.JSONDecoder()
    for m in _GLOBAL_RE.finditer(js):
        name = m.group(1) or m.group(2)
        if _NOISE_RE.search(name):
            continue
        rest = js[m.end():]
        try:
            pm = _JSON_PARSE_RE.match(rest)
            if pm:
                # JSON.parse("…escaped json…") / JSON.parse('…'): decode the string
                # literal first. Only double-quoted literals are valid JSON strings.
                lit, _ = dec.raw_decode(rest[pm.end():])
                value = json.loads(lit) if isinstance(lit, str) else None
            elif rest[:1] in ("{", "["):
                value, _ = dec.raw_decode(rest)
            else:
                continue
        except ValueError:
            continue
        if value is not None and name not in found:
            found[name] = value
    return found


def extract_state(html: str) -> dict:
    """Embedded JSON state from a page's <script> tags.

    - JSON-LD blocks          -> "json_ld": [block, ...] (page order)
    - <script type="application/json" id="X"> (e.g. __NEXT_DATA__, __NUXT_DATA__)
                              -> "X" (or its first data-* value; else application_json_N)
    - `window.__X = {...}` / `JSON.parse("...")` globals -> "__X"

    On any failure returns what was collected so far; never raises."""
    out: dict = {}
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        json_ld: list = []
        n_unnamed = 0
        for s in soup.find_all("script"):
            typ = (s.get("type") or "").split(";")[0].strip().lower()
            text = s.string if s.string is not None else s.get_text()
            if not text or not text.strip():
                continue
            if typ == "application/ld+json":
                value = _loads(text)
                if value is not None:
                    json_ld.append(value)
            elif typ == "application/json":
                value = _loads(text)
                if value is None:
                    continue
                key = s.get("id") or next(
                    (v for k, v in s.attrs.items() if k.startswith("data-") and isinstance(v, str) and v),
                    None)
                if not key:
                    n_unnamed += 1
                    key = f"application_json_{n_unnamed}"
                if not _NOISE_RE.search(key):
                    out.setdefault(key, value)
            elif typ in _SCRIPT_JS_TYPES and "__" in text:
                for name, value in _globals_in(text).items():
                    out.setdefault(name, value)
        if json_ld:
            out["json_ld"] = json_ld
    except Exception as e:
        logger.warning(f"state extraction failed: {e}")
    return out
