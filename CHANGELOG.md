# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic-ish
versioning while pre-1.0.

## [Unreleased]

## [0.3.0] - 2026-06-27

### Added
- **`unavailable` tier outcome** — when a tier's optional dependency is missing,
  the wrong version, or not installed yet (frozen PyPI `cloudscraper` instead of
  the 3.x stealth fork; patchright's Chromium not downloaded during an async
  cold-start install), the tier now fails fast (~0ms) with a distinct
  `unavailable` outcome carrying the exact install command, logged once per tier.
  It ranks above bot-wall in the verdict, so an environment problem is no longer
  masked as `botwall` — and a missing Tier 2 dependency no longer burns the
  per-URL solve budget before the browser tier runs.
- **`switchback --doctor`** — preflight tier-readiness check (doubles as a
  healthcheck: exit 0 when the capable tiers are ready). Reports whether
  cloudscraper is the stealth-capable 3.x fork, patchright + Chromium are
  installed, Camoufox/Node are present, and Firecrawl is configured. Built for
  cold-start deploys where the browser is installed by a background thread after
  boot.

### Docs
- README **Production / cold-start deployment** section and a `.env.example`
  Tier 2 block: install `patchright install chromium` in the post-boot step, the
  cloudscraper 3.x fork requirement, Node.js for Tier 2 concurrency, and the
  `SCRAPER_CLOUDSCRAPER_TIMEOUT_S` budget knob.

## [0.2.0] - 2026-06-25

### Added
- **Selectable output formats** — `SCRAPER_OUTPUT_FORMAT` (or per-call
  `scrape(fmt=...)`, CLI `--format`, `/scrape` `{"format": ...}`) selects the
  content shape: `markdown` (default, unchanged), `markdown_trimmed` (extra
  ad/nav/boilerplate removed), `html` (raw), or `html_selectors` (cleaned HTML
  with per-domain `drop`/`selector` applied). Default output is byte-identical;
  html-family results use a `html` JSON key instead of `markdown`.

## [0.1.0] - 2026-06-23

### Added
- **Challenge-type learning** — bot-walls are classified by vendor (Cloudflare,
  DataDome, Akamai, PerimeterX, Incapsula, Google) and counted per host in the
  botwall DB; the vendor is attached to each event and OTel span (`scrape.challenge`).
- **Metrics & reporting** — `switchback.reporting` rolls the event log + botwall DB
  into cost-savings-vs-Firecrawl, coverage, overall/per-tier/per-domain latency
  (mean/median/min/max/p50/p95), outcomes, error codes by domain, and challenges
  by domain. Exposed via `GET /metrics` and `GET /metrics/domains` (both accept
  `?minutes=N`).
- **Periodic flagging** — `python -m switchback.flags` emits a cron-friendly digest
  (domains stuck on Firecrawl, escalated to egress, most-challenged) to logs/OTel.
- **Content cache** — optional URL→result cache (`SCRAPER_CONTENT_TTL_S`, sqlite,
  off by default) short-circuits re-scrapes before any tier runs.
- **Login-session refresh** — `SCRAPER_LOGIN_HOOK` (`pkg.module:func`) refreshes a
  dead logged-in session on demand; cookies overlay every tier and persist.
- **Exponential backoff** — between-tier backoff with jitter after rate-limit /
  timeout (`SCRAPER_BACKOFF_BASE_MS` / `SCRAPER_BACKOFF_MAX_MS`, off by default).
- **Per-domain extraction prefs** — `config/extraction.json` (CSS scope selector +
  extra drops) applied automatically in the normalize step for every tier.
- **Session traces** — opt-in Playwright trace capture (`SCRAPER_TRACE_SESSION=1`)
  for browser tiers, with `GET/DELETE /traces` management endpoints.

### Changed
- Tier 2's `cloudscraper` moved from a core dependency (which pinned a git-URL
  fork PyPI can't publish) to the `cloudflare` extra; see the README for installing
  the 3.x Enhanced Edition fork for full stealth.
