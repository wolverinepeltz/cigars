#!/usr/bin/env python3
"""
gtcigars_deal_alert.py - email new deals from gtcigars.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds GT's settings. Each size is tracked separately, and email links open
the product with that size selected.

    python gtcigars_deal_alert.py --dry-run
    python gtcigars_deal_alert.py --collections daily-deals weekly-deals
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="GT Cigars",
    base_url="https://gtcigars.com",
    history_file=Path(__file__).parent / "gtcigars_sent_history.json",
    # shop-all-cigars is the full catalog; the deal collections are added in
    # case a promo item isn't filed under it. Duplicates are removed.
    collections=["shop-all-cigars", "daily-deals", "weekly-deals", "monthly-deals"],
    defaults={
        "min_discount": 35, "max_price": 125,
        # second rule: 25%+ off between $100 and $125
        "alt_min_discount": 25, "alt_min_price": 100, "alt_max_price": 125,
    },
)

if __name__ == "__main__":
    run(STORE)
