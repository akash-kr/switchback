"""The cost-ordered cascade. Each tier exposes:

    NAME : str
    PAID : bool                       # gated/audited if True
    fetch(url) -> str | None          # markdown on success; None if not
                                      # applicable; raises on failure.

Order matters — cheapest/cleanest first, paid last.
"""
from . import (tier0_apis, tier1_http, tier2_cloudscraper,
               tier3_browser, tier3b_camoufox, tier_residential, tier4_firecrawl)

TIERS = [
    tier0_apis,
    tier1_http,
    tier2_cloudscraper,
    tier3_browser,
    tier3b_camoufox,   # env-gated Firefox stealth (off by default; orthogonal to T3)
    tier_residential,  # residential-IP CDP browser (off unless BU_CDP_URL set)
    tier4_firecrawl,
]

# tier name -> index, for botwall winning-tier routing.
INDEX = {t.NAME: i for i, t in enumerate(TIERS)}
