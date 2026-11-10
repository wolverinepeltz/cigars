#!/usr/bin/env python3
"""
cigarsboston_deal_alert.py - email new deals from cigarsboston.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds Cigars Boston's settings. The store lists its own name as every
product's vendor, so brands are read from product tags and names instead.

    python cigarsboston_deal_alert.py --dry-run
    python cigarsboston_deal_alert.py --collections premium-cigar-special-deals
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="Cigars Boston",
    base_url="https://cigarsboston.com",
    history_file=Path(__file__).parent / "cigarsboston_sent_history.json",
    # premium-cigar-blends is the full cigar catalog (about 3,400); special
    # deals and samplers are added in case one isn't filed under it.
    # Duplicates are removed.
    collections=["premium-cigar-blends", "premium-cigar-special-deals",
                 "cigar-sample-packs"],
    defaults={"min_discount": 30, "max_price": 150},
    vendor_is_brand=False,
)

if __name__ == "__main__":
    run(STORE)
