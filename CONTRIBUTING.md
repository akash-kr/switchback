# Contributing

Thanks for your interest in improving switchback.

## Development setup
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"
patchright install chromium && camoufox fetch   # for the browser tiers
```

## Architecture
The engine is a cost-ordered cascade (`switchback/tiers/`) governed by a per-host
policy (`switchback/policy/`). Start with the cascade runner in
`switchback/orchestrator.py`.

## Guidelines
- **Keep the core small.** Each tier imports its deps lazily and a missing dep is
  just a tier miss — keep heavy/paid/optional pieces behind extras in
  `pyproject.toml`.
- **Make new behavior configurable and off-safe.** New features should be gated by
  an env var that defaults to current behavior (see the existing `SCRAPER_*` vars).
- **Match the surrounding style** — terse, comment-the-why, no speculative
  abstractions.
- **Don't commit secrets or run artifacts.** `.env`, `state/`, and `*.csv` are
  gitignored; keep it that way.

## Backward compatibility comes first
Upgrading must never change what an existing call returns. Breaking a user's app
is worse than any feature is good.

What went wrong once: the agent extras added `state` / `appendix` / `highlights`
as defaulted fields on `ScrapeResult` and `ScrapeOutcome`. That looked additive,
but `asdict(r)` (how people serialise results) gained three `null` keys and
`astuple(r)` grew from 4 values to 7. The CLI and HTTP JSON were fine; the Python
objects were not. It was caught before merge and fixed with opt-in subclasses.

Rules for any new feature:
- **Never add, remove, rename or reorder fields on a public result type**
  (`ScrapeResult`, `ScrapeOutcome`, `TierAttempt`, `SearchResult`). Put new data
  on a subclass that is returned only when the caller opts in (see
  `ScrapeResultWithExtras` in `switchback/orchestrator.py`).
- **New parameters are keyword-only and default to the old behaviour.** Existing
  parameters keep their names, order and defaults.
- **CLI and HTTP JSON gain a key only when the request asked for it** (see
  `result_dict` in `switchback/api.py`).
- **A new dependency gets its own extra and stays out of `[all]`**, and a missing
  dependency means the old behaviour. Installing a package is not opt-in: users
  may already have it for something else. Anything that changes what an existing
  call returns stays off until a `SCRAPER_*` variable turns it on (see
  `SCRAPER_YOUTUBE`, `SCRAPER_DOCUMENTS`).
- **The pinned tests are the contract.** The "backward compatibility" tests in
  `tests/test_extras.py` pin the result fields and the CLI/HTTP JSON keys, and CI
  runs them. If one fails, the change is breaking: redesign the change, don't
  edit the pin.
- **Before opening the PR, compare against `main` live:** scrape a few pages
  with the default call on both; the output should be byte-identical.

## Tests
`pytest tests` runs the offline tests (fake tiers, no network, a few seconds);
CI runs them on every PR. `tests/test_suite.py` exercises the cascade across the anti-bot difficulty
spectrum (needs network + browser tiers): `python tests/test_suite.py --quick`
for a fast tier-0/1 pass.

## Pull requests
Keep PRs focused; describe what changed and why, and note any new env var or
endpoint in the README and CHANGELOG.
