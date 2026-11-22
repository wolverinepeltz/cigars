#!/usr/bin/env python3
"""
cigarfederation_deal_alert.py - email new deals from store.cigarfederation.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds Cigar Federation's settings. Most products list the maker as vendor. The catalog also carries pipe
tobacco and accessories, which are left out by product type.
Each size is its own item.

    python cigarfederation_deal_alert.py --dry-run
    python cigarfederation_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="Cigar Federation",
    base_url="https://store.cigarfederation.com",
    history_file=Path(__file__).parent / "cigarfederation_sent_history.json",
    # "all" is Shopify's built-in collection of every product.
    collections=["all"],
    defaults={"min_discount": 30, "max_price": 150},
    skip_types={"pipe tobacco", "lighter", "cutter", "humidification", "ashtray",
                "cigar case", "hat", "apparel", "accessories", "humidor"},
)

if __name__ == "__main__":
    run(STORE)
