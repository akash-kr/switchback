# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic-ish
versioning while pre-1.0.

## [Unreleased]

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
