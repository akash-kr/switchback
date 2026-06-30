"""The cost-ordered cascade. Each tier exposes:

    NAME : str
    PAID : bool                       # gated/audited if True
    fetch(url) -> str | None          # markdown on success; None if not
                                      # applicable; raises on failure.

Order matters — cheapest/cleanest first, paid last.
"""
from . import (tier_1, tier_2, tier_3, tier_4, tier_5, tier_6, tier_7)

# Cost-ordered, cheapest first. Plain names; role noted inline.
TIERS = [
    tier_1,   # direct APIs / open mirrors
    tier_2,   # plain HTTP with TLS impersonation
    tier_3,   # cloudscraper (Cloudflare/anti-bot solver)
    tier_4,   # stealth headless browser (patchright)
    tier_5,   # camoufox (Firefox stealth; on by default, orthogonal to tier_4)
    tier_6,   # residential-IP CDP browser (off unless BU_CDP_URL set)
    tier_7,   # Firecrawl (paid, last resort)
]

# tier name -> index, for botwall winning-tier routing.
INDEX = {t.NAME: i for i, t in enumerate(TIERS)}
