<!-- switchback -->

```
███████╗██╗    ██╗██╗████████╗ ██████╗██╗  ██╗██████╗  █████╗  ██████╗██╗  ██╗
██╔════╝██║    ██║██║╚══██╔══╝██╔════╝██║  ██║██╔══██╗██╔══██╗██╔════╝██║ ██╔╝
███████╗██║ █╗ ██║██║   ██║   ██║     ███████║██████╔╝███████║██║     █████╔╝
╚════██║██║███╗██║██║   ██║   ██║     ██╔══██║██╔══██╗██╔══██║██║     ██╔═██╗
███████║╚███╔███╔╝██║   ██║   ╚██████╗██║  ██║██████╔╝██║  ██║╚██████╗██║  ██╗
╚══════╝ ╚══╝╚══╝ ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝
```

<div align="center">

**One cost-ordered scrape cascade — HTTP → stealth browser → paid — shared by every tool.**

Give it a URL; it tries the cheapest way to get clean Markdown first and only escalates
to a heavier (slower, costlier) tier when the cheap one is walled. Stops at the first success.

[![PyPI](https://img.shields.io/pypi/v/switchback)](https://pypi.org/project/switchback/)
[![Python](https://img.shields.io/pypi/pyversions/switchback)](https://pypi.org/project/switchback/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/akash-kr/switchback/actions/workflows/ci.yml/badge.svg)](https://github.com/akash-kr/switchback/actions/workflows/ci.yml)

</div>

---

## Why

Most scrapers either give up on hard pages or send *everything* through an expensive
headless browser / paid API. **switchback** orders the methods by cost and walks them
cheapest-first, per host, learning which tier wins where so the next run starts there.
The easy majority stays free; only genuinely-walled hosts pay for the heavy tiers.

- **Cost-ordered cascade** — free APIs → cheap HTTP → anti-bot solver → stealth browser → paid API.
- **Per-host memory (botwall)** — remembers the winning tier per host, skip-lists hard blockers, auto-skips hosts stuck on the paid tier.
- **Cost-scoped residential egress** — routes *only* walled hosts through a residential proxy, never the easy majority.
- **One shape, three entry points** — Python library, CLI (JSON on stdout), or an HTTP service.
- **Observable** — every attempt is an OpenTelemetry span; logs ship trace-correlated to any OTLP backend (Jaeger, Tempo, SigNoz).
- **Runs with any subset installed** — each tier imports its deps lazily; a missing one is just a tier miss.

## Quickstart

```bash
pip install switchback                 # core: cheap tiers (0/1) + search
```

```python
from switchback import scrape

for r in scrape(["https://arxiv.org/abs/1706.03762"]):
    print(r.source_method, len(r.markdown))
```

```bash
python -m switchback https://example.com/article    # JSON on stdout — bridge for any language
```

That's the whole loop. Add tiers as you need them (see [Install](#install)).

## The cascade (stop at first success)

| Tier | Strategy | Cost |
|---|---|---|
| 0 | Direct APIs / mirrors (arxiv, wikipedia, EuropePMC; extend: job boards) | free, cleanest |
| 1 | Plain HTTP + TLS impersonation (`curl_cffi`), incl. PDFs | cheap |
| 2 | Cloudflare / anti-bot solver (`cloudscraper`, install `.[cloudflare]`) | cheap-ish (~5s/host) |
| 3 | Stealth headless browser (`patchright`, Chromium) | heavy |
| 3b | Camoufox (Firefox stealth) — **on by default** (opt out: `SCRAPER_DISABLE_CAMOUFOX`) | heavy + slow (~40s on hard CF) |
| 3c | Residential-IP browser over CDP (`BU_CDP_URL`) — off unless configured | heavy (remote egress) |
| 4 | Firecrawl (paid, env-gated, audited) | paid, last resort |

Every URL has a wall-clock budget (`SCRAPER_DEADLINE_S`, default 45s) checked between
tiers so one URL can't run the whole cascade of timeouts. Each tier attempt records
latency + outcome (`ok` / `short_content` / `rate_limited` / `miss` / `not_applicable`)
to its span and the botwall event log; the root span carries total latency and the final
outcome (incl. `deadline_exceeded`).

Search (query → URLs) is separate from the scrape cascade: `switchback.search()` /
`python -m switchback.api --search <query>`, backed by a local SearXNG.


## Install

```bash
pip install switchback                 # core: normalization + cheap tiers (0/1) + search
pip install "switchback[cloudflare]"   # + Tier 2 Cloudflare/anti-bot solver (cloudscraper)
pip install "switchback[server]"       # + HTTP service (fastapi, uvicorn) incl. /metrics + /traces
pip install "switchback[browser]" && patchright install chromium   # + Tier 3 stealth Chromium
pip install "switchback[camoufox]" && camoufox fetch               # + Tier 3b Firefox stealth
pip install "switchback[firecrawl]"    # + Tier 4 paid API (needs FIRECRAWL_API_KEY)
pip install "switchback[tracing]"      # + OpenTelemetry -> any OTLP backend
pip install "switchback[all]"          # everything
```

For Tier 2's **full** v3 JS-VM + Turnstile + stealth, install the Enhanced Edition
3.x fork (PyPI's `cloudscraper` is the older v1/v2 — PyPI forbids pinning a
git-URL dep inside a published package, so install it alongside):

```bash
pip install "cloudscraper @ git+https://github.com/VeNoMouS/cloudscraper@3.0.0"
```

Or run the whole thing as a container:
`docker build -t switchback . && docker run -p 8799:8799 switchback`.

## Use it from your app

Three interchangeable entry points — all return the same shape
(`[{url, source_method, markdown}]`, successes only):

**Python library**
```python
from switchback import scrape
for r in scrape(["https://arxiv.org/abs/1706.03762"]):
    print(r.source_method, len(r.markdown))

# Need failures + reasons too? scrape_detailed returns a ScrapeOutcome per URL
# (ok, final_outcome, error_class, status_code, and the per-tier attempts):
from switchback import scrape_detailed
for o in scrape_detailed(["https://www.pcmag.com/news"]):
    if not o.ok:
        print(o.url, o.final_outcome, o.error_class, o.status_code)
```

**CLI** (JSON on stdout — bridge for any language)
```bash
python -m switchback https://example.com/article        # or: switchback <url>
```

**HTTP service** (language-agnostic; one warm process keeps the browser pool hot)
```bash
switchback-server                                    # listens on :8799
curl -s localhost:8799/scrape -d '{"urls":["https://example.com"]}'
curl 'localhost:8799/search?q=web+scraping'
```

Non-Python callers: see [clients/node_bridge.md](clients/node_bridge.md). Python
callers that want HTTP-with-CLI-fallback can drop in
[clients/python_client.py](clients/python_client.py).

## Cost-scoped residential egress

The dominant reason hard hosts wall you is the **datacenter IP**, not the
fingerprint. When a host repeatedly walls the local tiers (a 403/429 or a
bot-wall page, `SCRAPER_BOTWALL_EGRESS_AFTER` times) it's flagged `needs_egress`
and the cascade reruns through a **residential proxy** — but only for that host:

```bash
export SCRAPER_EGRESS_PROXY="http://user:pass@p.webshare.io:80"
```

The easy majority that already succeeds free at the datacenter IP stays direct,
so you never spend (often metered) residential bandwidth on it. Escalation tries
the cheap HTTP tiers through the proxy first (~0.2MB/page) before the heavier
browser tiers. [Webshare](https://www.webshare.io/)'s free plan includes ~1GB/mo
of residential bandwidth — enough for low-volume hard-host recovery at $0. Use
`SCRAPER_PROXY` instead to force *every* request through a proxy.

## Metrics & reporting

The engine derives all metrics from its own state files (no external store): the
botwall event log (one row per tier attempt, incl. the detected challenge vendor)
and the per-host DB (winning tier, per-vendor `challenge_counts`).

```bash
curl localhost:8799/metrics            # cost savings vs Firecrawl, coverage,
                                       # overall + per-tier latency, outcomes
curl localhost:8799/metrics/domains    # per-domain: error codes, challenges, latency
python -m switchback.flags             # periodic digest: domains stuck on Firecrawl,
                                       # escalated to egress, top challenged (cron-friendly)
```

Both endpoints accept `?minutes=N` to window the event-derived sections. The
**savings** figure compares engine spend (Firecrawl invocations only) against a
Firecrawl-everything baseline, charging the hard-page credit multiplier
(`BENCH_FIRECRAWL_HARD_MULT`) for URLs that needed a browser/residential tier or
hit a challenge — i.e. exactly the ones Firecrawl bills more for.

## Configuration

All configuration is via environment variables. The engine runs with missing
pieces: each tier imports its deps lazily and a missing one just counts as a tier
miss. Tracing no-ops if OTel isn't installed/configured.

<details>
<summary><b>Tracing (optional)</b></summary>

```bash
export OTEL_SERVICE_NAME=switchback
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```
</details>

<details>
<summary><b>Env gates</b> — enable/disable tiers and integrations</summary>

- `SCRAPER_DISABLE_FIRECRAWL` — skip Tier 4
- `FIRECRAWL_API_KEY` — enable Tier 4
- `SCRAPER_DISABLE_CAMOUFOX` — turn off Tier 3b (on by default; needs `pip install camoufox` + `camoufox fetch`)
- `BU_CDP_URL` — enable Tier 3c residential browser by pointing at a CDP endpoint
- `SCRAPER_PROXY` — route *all* tiers/URLs through a proxy
- `SCRAPER_EGRESS_PROXY` — route only walled hosts through a proxy (see [Cost-scoped residential egress](#cost-scoped-residential-egress))
- `SEARXNG_URL` — defaults to `http://localhost:8888`
- `SCRAPER_STATE_DIR` — where the botwall DB/event log + session cache live
- `SCRAPER_COOKIES_FILE` — Netscape `cookies.txt` to scrape login-gated hosts (injected into the HTTP and browser tiers)
- `SCRAPER_CAPTCHA_PROVIDER` + `SCRAPER_CAPTCHA_API_KEY` — opt-in, off by default: wire a third-party solver (2captcha/capsolver/capmonster/anticaptcha/deathbycaptcha/9kw) into Tier 2 for Turnstile/reCAPTCHA/hCaptcha on CF hosts. **Paid**, billed per solve by the provider.
</details>

<details>
<summary><b>Tunables</b> — budgets, timeouts, caches, backoff</summary>

- `SCRAPER_OUTPUT_FORMAT` — output shape: `markdown` (default) · `markdown_trimmed` · `html` · `html_selectors` (see [Output formats](#output-formats))
- `SCRAPER_DEADLINE_S` — per-URL budget (45s)
- `SCRAPER_CAMOUFOX_TIMEOUT_MS` — (45000)
- `SCRAPER_BROWSER_CONCURRENCY` — max simultaneous headless browsers (default 1)
- `SCRAPER_BOTWALL_URL_SKIP_COOLDOWN_H` — auto-skip re-test window (24h; 0 = never)
- `SCRAPER_BOTWALL_EGRESS_AFTER` — local-tier failures before a host escalates to the residential tier (default 2)
- `SCRAPER_SESSION_TTL_S` — cf_clearance reuse window (1800s)
- `SCRAPER_DISABLE_SESSION_CACHE` — turn off cf_clearance reuse
- `SCRAPER_CONTENT_TTL_S` — URL→result cache TTL (**0 = off**; set e.g. 86400 to skip re-scraping a page within a day)
- `SCRAPER_BACKOFF_BASE_MS` / `SCRAPER_BACKOFF_MAX_MS` — exponential backoff between tiers after a rate-limit/timeout (base 0 = off)
- `SCRAPER_LOGIN_HOOK` — `pkg.module:func` returning `{cookie: value}` for a host (see [Logged-in sessions](#logged-in-sessions))
- `SCRAPER_EXTRACTION_FILE` — per-domain extraction prefs JSON (default `config/extraction.json`)
- `SCRAPER_TRACE_SESSION` — opt-in: capture a Playwright trace (screenshots + DOM + network) per browser-tier attempt, written to `state/traces/`
- `BENCH_FIRECRAWL_USD` / `BENCH_FIRECRAWL_HARD_MULT` — cost model for the savings report
</details>

### Logged-in sessions
Beyond a static `SCRAPER_COOKIES_FILE`, wire `SCRAPER_LOGIN_HOOK` to a callable
`func(host) -> {cookie: value}`. When an authenticated host trips a login/bot
wall, the engine calls the hook once, persists the returned cookies per host, and
overlays them on every tier (and future runs), then re-runs that URL on a fresh
budget. The hook owns the site-specific login mechanics; the engine stays generic.

### Testing the session/cookie machinery

`tests/test_session_nyt.py` is an integration suite for the session/cookie
machinery, exercised end-to-end against **nytimes.com**. It has two layers:

- **Machinery tests** (always run, offline, no credentials): cookies.txt
  injection, cf_clearance reuse + TTL eviction, overlay precedence
  (auth < refreshed-login < cf cache), the login-hook refresh path, egress-scope
  isolation, and the `SCRAPER_DISABLE_SESSION_CACHE` switch.
- **Live NYT flow** (skipped unless credentials are present): real login →
  persisted session → gated content reachable *with* a session and not without,
  and session reuse skipping re-login within the TTL.

Run the suite:

```bash
pip install -e ".[browser]" && patchright install chromium   # for the live login
pip install pytest
pytest tests/test_session_nyt.py -v
```

**To run the live flow, add your NYT credentials** — copy `.env.example` to
`.env` (gitignored) and fill in **both** vars:

```bash
cp .env.example .env
# then edit .env:
NYT_USERNAME=you@example.com
NYT_PASSWORD=your-password
```

The test fixtures load `.env` automatically. With the vars blank, the live tests
skip and CI stays green; with both set, they log in and run the real flow. The
login itself is driven by `tests/nyt_login.py` (the `SCRAPER_LOGIN_HOOK` target)
— if NYT changes its login form, update the selectors there. Credentials live
only in your local `.env`; never commit them or paste them anywhere else.

### Session traces
With `SCRAPER_TRACE_SESSION=1`, each browser-tier attempt writes a Playwright
trace zip to `state/traces/`. Manage them over HTTP — `GET /traces` (list),
`GET /traces/{id}` (download), `DELETE /traces/{id}` — and open one with
`playwright show-trace <zip>`. Off by default (traces are MBs each).

### Output formats
Markdown is the default and is unchanged. Pick a different shape globally with
`SCRAPER_OUTPUT_FORMAT`, or per call:

```python
from switchback import scrape
scrape(["https://example.com/article"])                    # markdown (default)
scrape(["https://example.com/article"], fmt="html")        # raw HTML
scrape(["https://example.com/article"], fmt="markdown_trimmed")
```

```bash
switchback --format html_selectors https://example.com/article
curl -s localhost:8799/scrape -d '{"urls":["https://example.com"],"format":"html"}'
```

| format | what you get |
| --- | --- |
| `markdown` | whole-page markdown (boilerplate stripped + per-domain prefs) — **default** |
| `markdown_trimmed` | markdown with extra ad/nav/boilerplate lines removed |
| `html` | the raw HTML exactly as fetched, untouched |
| `html_selectors` | cleaned HTML (boilerplate strip + per-domain `drop`/`selector`), not converted |

The chosen content rides in the result's `markdown` field; in the CLI/server JSON
the key is `markdown` for markdown formats and `html` for html formats. The
API/PDF tiers (arXiv synth, PDF→text) have no HTML, so html formats fall back to
their text for those sources.

### Per-domain extraction
Markdown of the whole page is the default. To scope a site to its content node or
strip site-specific noise, declare prefs per host in `config/extraction.json`
(see [config/extraction.example.json](config/extraction.example.json)); every
tier's normalize step picks them up automatically.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Start with the
cascade runner in `switchback/orchestrator.py`.

## Responsible use

This engine is for lawful data collection. You are responsible for respecting
each target site's Terms of Service, `robots.txt`, and rate limits, and for
having the right to access the content you fetch. The stealth / anti-bot tiers
(`cloudscraper`, `patchright`, `camoufox`) exist to handle legitimate access
friction (e.g. generic bot interstitials on public pages) — not to evade access
controls, paywalls, or authentication you aren't authorized to bypass. The
software is provided "as is", without warranty (see [LICENSE](LICENSE)).

## License

MIT — see [LICENSE](LICENSE). Third-party dependencies and their licenses are
listed in [NOTICE](NOTICE); all are permissive (MIT / BSD-3-Clause / Apache-2.0)
and compatible with this project's MIT license.
