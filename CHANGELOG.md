# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic-ish
versioning while pre-1.0.

## [Unreleased]

## [0.5.0] - 2026-06-30

### Changed
- **Tiers renamed to plain `tier_1`…`tier_7`** (cost-ordered, contiguous) in place
  of the old mixed scheme (`tier0_apis`, `tier1_http`, `tier2_cloudscraper`,
  `tier3_browser`, `tier3b_camoufox`, `tier_residential`, `tier4_firecrawl`). The
  mapping is positional: `tier_1`=apis, `tier_2`=http, `tier_3`=cloudscraper,
  `tier_4`=browser, `tier_5`=camoufox, `tier_6`=residential, `tier_7`=firecrawl.
  **Backwards-compatible:** an existing `state/botwall_db.json` is migrated on load
  (a host's learned `winning_tier` / `tier_stats` keys are remapped to the new
  names), so routing survives the upgrade instead of re-probing from scratch.

### Added
- **Per-tier timeout knobs** — every tier now reads `SCRAPER_TIER_<N>_TIMEOUT_S`
  (seconds, `N` = 1–7). Defaults: `15` for tiers without a prior budget
  (apis/http/browser), and the existing budgets are preserved — `tier_3`=25,
  `tier_5`=45, `tier_6`=30. The previously-unconfigurable/unbounded tiers (apis,
  http, browser, **firecrawl**) are now bounded and overridable. The pre-rename
  `SCRAPER_CLOUDSCRAPER_TIMEOUT_S` / `SCRAPER_CAMOUFOX_TIMEOUT_MS` /
  `SCRAPER_RESIDENTIAL_TIMEOUT_MS` are still honored when the new var is unset.
  Note: `tier_7` (paid Firecrawl) was previously unbounded; its new `15`s default
  bounds it — raise `SCRAPER_TIER_7_TIMEOUT_S` if hard hosts get cut off (a scrape
  killed at the cap may still be billed). `SCRAPER_TIER_RETRIES_<TIER>` overrides
  follow the new names (e.g. `SCRAPER_TIER_RETRIES_TIER_4`).

## [0.4.0] - 2026-06-29

### Added
- **Configurable per-tier retries** — a tier can now re-attempt before falling
  through to the next, more capable one. `SCRAPER_TIER_RETRIES` (global, default
  `0` = off; `N` → up to `1+N` tries per tier), per-tier overrides
  `SCRAPER_TIER_RETRIES_<TIER>` (e.g. `SCRAPER_TIER_RETRIES_TIER3_BROWSER=2`), and
  `SCRAPER_TIER_RETRY_ON` (retryable failure classes; default
  `timeout,rate_limited,connection` — widen to include `botwall,http_block` behind
  a rotating residential proxy, where each retry gets a fresh IP). Retries stay
  bounded by `SCRAPER_DEADLINE_S`, and intermediate retries are traced/logged but
  **not** persisted to the botwall policy DB, so they never inflate the
  self-healing skip / `needs_egress` counters. Default `0` keeps behaviour
  unchanged. Enabling retries on the paid Firecrawl tier bills per attempt.

### Fixed
- **Quality gate rejects content shells** — the gate no longer passes a page just
  because it clears the length floor; thin "shell" pages (nav/boilerplate with no
  real article body) are now treated as a tier miss so the cascade falls through.
- **Paid last-resort budget reserve** — `SCRAPER_FIRECRAWL_FALLBACK_AFTER_S`
  (default 25s) stops starting local tiers once enough of the per-URL deadline has
  elapsed and an enabled paid tier is still ahead, so a hard host can't burn the
  whole budget before Firecrawl gets a turn.

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
