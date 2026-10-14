#!/usr/bin/env python3
"""Diagnose *why* a batch of URLs fell through the switchback cascade.

For every URL it runs the real cascade (Tier 0 APIs -> 1 HTTP -> 2 cloudscraper
-> 3 patchright -> 3b camoufox -> [residential] -> [firecrawl]) via
`scrape_detailed`, then prints the per-tier chain, a one-line human reason, and a
bucketed summary so you can see at a glance which wall stopped each URL.

Safe to share/run:
  * Firecrawl (the paid Tier 4) is DISABLED by default. A URL that would have
    escalated to Firecrawl is reported as `would_hit_firecrawl` so you can see
    the routing without spending credits. Pass --enable-firecrawl (and set
    FIRECRAWL_API_KEY) to actually exercise it.
  * State (the per-host botwall DB / event log) is written to a throwaway temp
    dir by default, so your real state/ is untouched. Override with --state-dir.

Usage:
  python scripts/diagnose_urls.py urls.txt              # one URL per line
  python scripts/diagnose_urls.py https://a.com https://b.com
  cat urls.txt | python scripts/diagnose_urls.py -      # from stdin
  python scripts/diagnose_urls.py urls.txt --json out.json --csv out.csv
  python scripts/diagnose_urls.py urls.txt --enable-firecrawl

Output:
  * live, per-URL line:  <ok/XX>  <reason>  won/died-at  md_len  chain
  * --json / --csv:      machine-readable per-URL records (incl. full chain)
  * trailing summary:    counts per reason bucket + per winning tier
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sys
import tempfile
import time
from collections import Counter
from urllib.parse import urlsplit


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+",
                   help="URL(s), a path to a newline-delimited file, or '-' for stdin")
    p.add_argument("--format", dest="fmt", default=None,
                   help="output format: markdown (default) | markdown_trimmed | html | html_selectors")
    p.add_argument("--enable-firecrawl", action="store_true",
                   help="actually run the paid Tier 4 (needs FIRECRAWL_API_KEY); off by default")
    p.add_argument("--verbose", action="store_true",
                   help="show the engine's per-tier INFO/WARNING logs (default: quiet)")
    p.add_argument("--state-dir", default=None,
                   help="botwall state dir (default: a throwaway temp dir, leaving real state/ untouched)")
    p.add_argument("--json", dest="json_out", default=None, help="write per-URL records to this JSON file")
    p.add_argument("--csv", dest="csv_out", default=None, help="write per-URL records to this CSV file")
    p.add_argument("--dump-dir", default=None,
                   help="save each successful result's content to this dir (one .md/.txt per URL), "
                        "so the output can be audited for false-positive 'successes' (garbage that passed the gate)")
    return p.parse_args()


def load_urls(inputs: list[str]) -> list[str]:
    """A token is a URL if it looks like one; otherwise treat it as a file path
    (or '-' for stdin). Lets you mix `urls.txt` and bare URLs on the CLI."""
    urls: list[str] = []
    for tok in inputs:
        if tok == "-":
            urls += [ln.strip() for ln in sys.stdin if ln.strip()]
        elif tok.startswith(("http://", "https://")):
            urls.append(tok)
        else:
            path = pathlib.Path(tok)
            if not path.exists():
                sys.exit(f"not a URL and not a file: {tok!r}")
            urls += [ln.strip() for ln in path.read_text().splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")]
    # de-dupe, keep order
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


# Map a finished ScrapeOutcome to a short, human "why" bucket. The cascade's own
# error_class is the dominant failing tier's outcome; we fold the terminal
# states into labels a non-author can read.
def reason_for(o) -> str:
    if o.ok:
        return "ok"
    ec = o.error_class
    if o.final_outcome == "deadline_exceeded":
        return "deadline_exceeded"
    if o.final_outcome in ("domain_skipped", "url_excluded"):
        return o.final_outcome
    if ec == "botwall":
        return "botwall_unsolved"          # CF/anti-bot challenge no tier cleared
    if ec == "http_block":
        code = o.status_code or "4xx"
        return f"hard_block_{code}"         # bare 403/401: IP/auth reputation block
    if ec == "rate_limited":
        return "rate_limited_429"
    if ec == "short_content":
        return "short_content"             # under the quality gate (JS-shell or genuinely short)
    return ec or o.final_outcome or "all_failed"


def firecrawl_attempt(o):
    """Return the Tier-4 attempt's outcome if the cascade actually reached
    Firecrawl (not gated/skipped), else None."""
    for a in o.attempts:
        if a.tier == "tier4_firecrawl" and a.outcome not in ("disabled", "not_applicable"):
            return a.outcome
    return None


def chain_str(o) -> str:
    parts = []
    for a in o.attempts:
        tag = a.tier.replace("tier", "t").replace("_", ":")
        s = f"{tag}:{a.outcome}"
        if a.status_code:
            s += f"[{a.status_code}]"
        parts.append(s)
    return " -> ".join(parts)


def main() -> int:
    args = parse_args()

    # --- safety env: set BEFORE importing switchback (gates read env at import) ---
    if not args.enable_firecrawl:
        os.environ["SCRAPER_DISABLE_FIRECRAWL"] = "1"
        os.environ.pop("FIRECRAWL_API_KEY", None)
    state_dir = args.state_dir or tempfile.mkdtemp(prefix="switchback-diag-")
    os.environ["SCRAPER_STATE_DIR"] = state_dir

    # import here so the env above is in effect
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    if not args.verbose:
        import logging
        logging.getLogger("switchback").setLevel(logging.ERROR)
    from switchback import scrape_detailed  # noqa: E402

    urls = load_urls(args.inputs)
    fc = "ON (paid!)" if args.enable_firecrawl else "off"
    print(f"# diagnosing {len(urls)} URL(s) | firecrawl={fc} | state_dir={state_dir}\n",
          file=sys.stderr)

    dump_dir = None
    if args.dump_dir:
        dump_dir = pathlib.Path(args.dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)

    records = []
    t_start = time.time()
    for i, url in enumerate(urls, 1):
        t0 = time.time()
        o = scrape_detailed([url], fmt=args.fmt)[0]
        dt = time.time() - t0
        if dump_dir is not None and o.ok and o.markdown:
            ext = "html" if "html" in (o.format or "") else "md"
            (dump_dir / f"{i:03d}_{urlsplit(url).hostname}.{ext}").write_text(o.markdown)
        reason = reason_for(o)
        fc_outcome = firecrawl_attempt(o)
        reached_fc = fc_outcome is not None
        won_fc = o.ok and o.source_method == "tier4_firecrawl"
        # When firecrawl is off, an exhausted cascade is exactly what would have
        # been handed to firecrawl in production — label it so the routing is visible.
        terminal = o.final_outcome in ("all_failed", "deadline_exceeded")
        if won_fc:
            routed = "saved_by_firecrawl"
        elif reached_fc:                      # reached fc but fc also failed
            routed = "firecrawl_failed"
        elif terminal and not args.enable_firecrawl:
            routed = "would_hit_firecrawl"
        else:
            routed = o.final_outcome
        rec = {
            "n": i,
            "url": url,
            "host": urlsplit(url).hostname or "",
            "ok": o.ok,
            "reason": reason,
            "final_outcome": o.final_outcome,
            "routed": routed,
            "won_tier": o.source_method,
            "reached_firecrawl": reached_fc,
            "firecrawl_outcome": fc_outcome or "",
            "status_code": o.status_code,
            "md_len": len(o.markdown),
            "latency_s": round(dt, 1),
            "chain": chain_str(o),
        }
        records.append(rec)
        flag = "OK" if o.ok else "XX"
        where = o.source_method if o.ok else routed
        print(f"[{i:3d}/{len(urls)}] {flag} {dt:5.1f}s  {reason:18s} "
              f"{where:20s} md={rec['md_len']:6d}  {rec['host']}")
        print(f"            {rec['chain']}")

    total = time.time() - t_start

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(records, indent=2))
    if args.csv_out:
        with open(args.csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            w.writeheader()
            w.writerows(records)

    # --- summary ---
    print("\n" + "=" * 72)
    print(f"SUMMARY  ({len(urls)} URLs in {total:.0f}s)")
    ok = sum(r["ok"] for r in records)
    reached = [r for r in records if r["reached_firecrawl"]]
    saved = sum(r["routed"] == "saved_by_firecrawl" for r in records)
    fc_failed = sum(r["routed"] == "firecrawl_failed" for r in records)
    would = sum(r["routed"] == "would_hit_firecrawl" for r in records)
    print(f"  succeeded: {ok}/{len(urls)}")
    if args.enable_firecrawl:
        print(f"  reached Firecrawl (Tier 4): {len(reached)}   "
              f"-> saved by it: {saved}   firecrawl also failed: {fc_failed}")
    else:
        print(f"  would-hit-firecrawl: {would}")
    print("\n  by reason:")
    for reason, n in Counter(r["reason"] for r in records).most_common():
        print(f"    {n:3d}  {reason}")
    print("\n  successes by winning tier:")
    for tier, n in Counter(r["won_tier"] for r in records if r["ok"]).most_common():
        print(f"    {n:3d}  {tier}")
    print("\n  top failing hosts:")
    bad = Counter(r["host"] for r in records if not r["ok"])
    for host, n in bad.most_common(12):
        print(f"    {n:3d}  {host}")
    if args.enable_firecrawl and reached:
        print(f"\n  URLs that reached Firecrawl ({len(reached)}):")
        for r in reached:
            mark = "saved " if r["routed"] == "saved_by_firecrawl" else "FAILED"
            print(f"    [{mark}] fc:{r['firecrawl_outcome']:13s} {r['url']}")
    if args.json_out:
        print(f"\n  wrote {args.json_out}")
    if args.csv_out:
        print(f"  wrote {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
