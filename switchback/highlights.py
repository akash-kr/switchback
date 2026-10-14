"""Query highlights — the passages of a scraped page that answer a question.

    highlights(markdown, query, url) -> [{"text", "score", "grader", "link"}, ...]

The page markdown is cut into passages (a heading travels with the paragraphs
under it) and graded against the query:

  bm25  default. Local keyword scoring, no network, no cost. Returns the top
        passages that share terms with the query (score = BM25, unbounded).
  jev   opt-in: SCRAPER_HIGHLIGHTS_GRADER=jev + TYPESAFE_API_KEY. TypeSafe's Jev
        model answers "does this passage answer the query?" per passage and
        returns a calibrated probability (score = 0..1). Passages below
        SCRAPER_HIGHLIGHTS_MIN_SCORE (0.5) are dropped, so an off-topic page
        honestly returns []. Sends the passages + query to api.typesafe.ai.
        Any Jev failure falls back to bm25 for that call; the scrape never fails.

Every highlight carries a `link`: the page URL with a text fragment
(`#:~:text=start,end`) that scrolls a browser to, and highlights, that passage —
or, for a YouTube transcript, the video at that passage's timestamp.
"""
from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from urllib.parse import quote

logger = logging.getLogger(__name__)

TOP_K = int(os.getenv("SCRAPER_HIGHLIGHTS_TOP_K", "5"))
MIN_SCORE = float(os.getenv("SCRAPER_HIGHLIGHTS_MIN_SCORE", "0.5"))  # jev only

# Passage sizing, in words: grow a passage until TARGET, split anything past MAX.
_TARGET_WORDS = 80
_MAX_WORDS = 220

# ── Passages ────────────────────────────────────────────────────────────────


def _words(s: str) -> int:
    return len(s.split())


def _pack(units: list[str], sep: str, head: list[str] = ()) -> list[str]:
    """Greedily join units into pieces of ≤_MAX_WORDS words, each prefixed by head."""
    out, cur = [], list(head)
    for u in units:
        if len(cur) > len(head) and _words(sep.join(cur + [u])) > _MAX_WORDS:
            out.append(sep.join(cur))
            cur = list(head)
        cur.append(u)
    if len(cur) > len(head):
        out.append(sep.join(cur))
    return out


def _split_long(block: str) -> list[str]:
    """Split an oversized block into ≤_MAX_WORDS pieces: tables and lists by line
    (a table's header rows repeat on every piece so each stays readable), prose by
    sentence, and anything still too long by words."""
    lines = block.splitlines()
    if len(lines) > 1:
        # Header = anything above the table's |---| separator (title + header row),
        # when that separator is near the top.
        sep = next((i for i, l in enumerate(lines[:4])
                    if i and re.fullmatch(r"\s*\|?[\s:|-]*-[\s:|-]*\|?\s*", l)
                    and lines[i - 1].lstrip().startswith("|")), None)
        head = lines[:sep + 1] if sep is not None else []
        pieces = _pack(lines[len(head):], "\n", head)
    else:
        pieces = _pack(re.split(r"(?<=[.!?])\s+", block), " ")
    out = []
    for p in pieces:
        out.extend(_pack(p.split(" "), " ") if _words(p) > _MAX_WORDS * 1.5 else [p])
    return out


def passages(markdown: str) -> list[str]:
    """Cut markdown into passages of roughly _TARGET_WORDS words. A heading starts
    a new passage and stays attached to the text under it."""
    out: list[str] = []
    buf: list[str] = []

    def flush():
        text = "\n\n".join(buf).strip()
        if text and not re.fullmatch(r"(#+\s*[^\n]*)", text):  # skip heading-only
            out.append(text)
        buf.clear()

    for block in re.split(r"\n\s*\n", markdown or ""):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            flush()
        for piece in (_split_long(block) if _words(block) > _MAX_WORDS else [block]):
            # Close the passage before it would overflow, unless it's just a heading.
            if (buf and _words("\n\n".join(buf + [piece])) > _MAX_WORDS
                    and not (len(buf) == 1 and buf[0].startswith("#"))):
                flush()
            buf.append(piece)
            if _words("\n\n".join(buf)) >= _TARGET_WORDS:
                flush()
    flush()
    return out


# ── BM25 (local, free) ──────────────────────────────────────────────────────

_STOP = frozenset("""a an and are as at be but by for from has have how i if in into
is it its of on or that the their there these this to was were what when where
which who why will with you your do does did can""".split())


def _stem(t: str) -> str:
    """Light suffix stripping so "countries"/"country", "listed"/"list" match."""
    for suf, rep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: len(t) - len(suf)] + rep
    return t


def _tokens(s: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _STOP]


def bm25_scores(chunks: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """BM25 score of each chunk for the query (0 when no query term appears)."""
    docs = [_tokens(c) for c in chunks]
    q = set(_tokens(query))
    if not docs or not q:
        return [0.0] * len(chunks)
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d) if t in q)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if tf[t]:
                s += idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        scores.append(s)
    return scores


# ── Jev (opt-in, TypeSafe API) ──────────────────────────────────────────────

_JEV_URL = os.getenv("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
_JEV_MODEL = os.getenv("SCRAPER_JEV_MODEL", "jev-latest")
_JEV_TIMEOUT_S = float(os.getenv("SCRAPER_JEV_TIMEOUT_S", "20"))
# Request sizing. Jev allows 64k tokens per request and 32k for the state plus
# the longest question; ~4 chars/token keeps a batch well inside both.
_JEV_BATCH_CHARS = 80_000
_JEV_BATCH_N = 60
_JEV_MAX_PASSAGES = 200  # larger pages are pre-ranked by bm25 down to this

_QUESTION_CRITERIA = {
    "true": "The passage states information that answers the question, or a specific part of it.",
    "false": "The passage is off-topic, or only mentions the topic without answering the question.",
}


def _jev_batches(chunks: list[str]) -> list[list[int]]:
    batches, cur, size = [], [], 0
    for i, c in enumerate(chunks):
        if cur and (size + len(c) > _JEV_BATCH_CHARS or len(cur) >= _JEV_BATCH_N):
            batches.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(c)
    if cur:
        batches.append(cur)
    return batches


def jev_scores(chunks: list[str], query: str, api_key: str) -> list[float]:
    """Jev's probability that each chunk answers the query. One Noul question per
    passage, batched so each request stays inside Jev's token budget. Raises on
    any API failure (the caller falls back to bm25)."""
    import requests
    scores = [0.0] * len(chunks)
    for batch in _jev_batches(chunks):
        state = {"query": query, "passages": [chunks[i] for i in batch]}
        questions = {
            f"p{j}": {
                "type": "noul",
                "instructions": (f"Does `passages[{j}]` contain information that answers "
                                 f"the question in `query`?"),
                "criteria": _QUESTION_CRITERIA,
            }
            for j in range(len(batch))
        }
        r = requests.post(_JEV_URL, timeout=_JEV_TIMEOUT_S,
                          headers={"Authorization": f"Bearer {api_key}"},
                          json={"model": _JEV_MODEL, "state": state, "questions": questions})
        r.raise_for_status()
        answers = r.json()["answers"]
        for j, i in enumerate(batch):
            scores[i] = float(answers[f"p{j}"]["noul"])
    return scores


def grader() -> str:
    """The configured grader: "jev" when selected and a TypeSafe key is set, else "bm25"."""
    want = os.getenv("SCRAPER_HIGHLIGHTS_GRADER", "bm25").strip().lower()
    if want == "jev" and os.getenv("TYPESAFE_API_KEY"):
        return "jev"
    if want == "jev":
        logger.warning("SCRAPER_HIGHLIGHTS_GRADER=jev but TYPESAFE_API_KEY is unset; using bm25")
    return "bm25"


# ── Citations ───────────────────────────────────────────────────────────────

# A link target: the URL (which may hold balanced parens) plus an optional "title".
_TARGET = r"\((?:[^()\s]|\([^()]*\))*(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)"
_MD_IMG = re.compile(r"!\[[^\]]*\]" + _TARGET)
_MD_LINK = re.compile(r"\[((?:\[[^\]]*\]|[^\[\]])*)\]" + _TARGET)  # incl. [[3]](url)
_MD_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_MD_MARKS = re.compile(r"(\*\*|__|~~|`|^\s*#+\s*|^\s*[-*>]\s+|^\s*\d+\.\s+|\|)", re.M)


def plain_text(md: str) -> str:
    """Markdown → the visible text a browser would match (links/images/marks off)."""
    s = _MD_IMG.sub(" ", md)
    s = _MD_LINK.sub(r"\1", s)
    s = _MD_AUTOLINK.sub(r"\1", s)
    s = _MD_MARKS.sub(" ", s)
    s = re.sub(r"(?<!\w)(?<!\\)[*_](?=\S)|(?<=\S)(?<!\\)[*_](?!\w)", "", s)  # emphasis marks
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", r"\1", s)  # markdownify's escapes
    return re.sub(r"\s+", " ", s).strip()


def _frag(s: str) -> str:
    # Text-fragment values must also escape '-', ',' and '&' (directive syntax).
    return quote(s, safe="").replace("-", "%2D")


# A transcript passage cites its moment in the video (tier_1's timestamp links),
# since the transcript text isn't on the video page for a text fragment to find.
_TIMESTAMP_LINK = re.compile(r"\[\d+(?::\d\d){1,2}\]\((https://www\.youtube\.com/watch\?v=[^)\s]+&t=\d+s)\)")


def cite_link(url: str, passage: str, query: str = "") -> str:
    """The citation for a passage: for a YouTube transcript, the timestamp of its
    paragraph that best matches the query; else a text-fragment link into the page."""
    if re.search(r"(?:youtube\.com|youtu\.be)/", url):
        q = set(_tokens(query))
        best = None  # (overlap, link); first paragraph wins ties
        for para in passage.split("\n\n"):
            m = _TIMESTAMP_LINK.search(para)
            if m:
                overlap = len(q & set(_tokens(para)))
                if best is None or overlap > best[0]:
                    best = (overlap, m.group(1))
        if best:
            return best[1]
    return fragment_link(url, passage)


def fragment_link(url: str, passage: str, edge_words: int = 5) -> str:
    """`url` + a text fragment pointing at `passage`. Uses its longest prose line
    (headings and table rows are skipped when there is prose, since their visible
    text rarely matches the markdown cell-for-cell): the whole line when short,
    else its first and last `edge_words` words as a start,end range."""
    raw = [l for l in passage.splitlines() if l.strip()]
    prose = [l for l in raw if not l.lstrip().startswith(("#", "|"))]
    line = max((plain_text(l) for l in (prose or raw)), key=len, default="")
    words = line.split()
    if not words:
        return url
    if len(words) <= edge_words * 2:
        directive = _frag(" ".join(words))
    else:
        directive = f"{_frag(' '.join(words[:edge_words]))},{_frag(' '.join(words[-edge_words:]))}"
    base, sep, frag = url.partition("#")
    return f"{base}#{frag}:~:text={directive}" if sep and frag else f"{base}#:~:text={directive}"


# ── Entry point ─────────────────────────────────────────────────────────────


def highlights(markdown: str, query: str, url: str) -> list[dict]:
    """The passages of `markdown` that best answer `query`, best first, each with
    its score, the grader that produced it, and a citation link into `url`."""
    chunks = passages(markdown)
    if not chunks or not (query or "").strip():
        return []
    bm25 = bm25_scores(chunks, query)
    used = grader()
    if used == "jev":
        # Grade every passage when the page is small enough, so Jev can find what
        # keyword matching misses; on huge pages pre-rank by bm25 first.
        pool = sorted(range(len(chunks)), key=lambda i: bm25[i], reverse=True)[:_JEV_MAX_PASSAGES]
        pool.sort()
        try:
            s = jev_scores([chunks[i] for i in pool], query, os.environ["TYPESAFE_API_KEY"])
            ranked = [(s[k], i) for k, i in enumerate(pool) if s[k] >= MIN_SCORE]
        except Exception as e:
            logger.warning(f"jev grading failed, using bm25 for this call: {e}")
            used = "bm25"
    if used == "bm25":
        ranked = [(sc, i) for i, sc in enumerate(bm25) if sc > 0]
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [{"text": chunks[i], "score": round(sc, 4), "grader": used,
             "link": cite_link(url, chunks[i], query)}
            for sc, i in ranked[:TOP_K]]
