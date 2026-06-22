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

## Tests
`tests/test_suite.py` exercises the cascade across the anti-bot difficulty
spectrum (needs network + browser tiers): `python tests/test_suite.py --quick`
for a fast tier-0/1 pass.

## Pull requests
Keep PRs focused; describe what changed and why, and note any new env var or
endpoint in the README and CHANGELOG.
