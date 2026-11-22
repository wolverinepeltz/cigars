#!/usr/bin/env python3
"""
iheartcigars_deal_alert.py - email new deals from iheartcigars.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds iHeartCigars's settings. Most products list the maker as vendor; the store's own blends list
"iHeartCigars".
Each size is its own item.

    python iheartcigars_deal_alert.py --dry-run
    python iheartcigars_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="iHeartCigars",
    base_url="https://iheartcigars.com",
    history_file=Path(__file__).parent / "iheartcigars_sent_history.json",
    # "all" is Shopify's built-in collection of every product.
    collections=["all"],
    defaults={"min_discount": 30, "max_price": 150},
    skip_types={"cigar accessory"},
)

if __name__ == "__main__":
    run(STORE)
