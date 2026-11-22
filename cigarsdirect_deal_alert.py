#!/usr/bin/env python3
"""
cigarsdirect_deal_alert.py - email new deals from cigarsdirect.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds Cigars Direct's settings. The whole catalog is about 7,000 products, so a run reads about 30 pages.
Accessories and merchandise are left out by product type.
Each size is its own item.

    python cigarsdirect_deal_alert.py --dry-run
    python cigarsdirect_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="Cigars Direct",
    base_url="https://www.cigarsdirect.com",
    history_file=Path(__file__).parent / "cigarsdirect_sent_history.json",
    # "all" is Shopify's built-in collection of every product.
    collections=["all"],
    defaults={"min_discount": 30, "max_price": 150},
    skip_types={"lighters", "swag", "hats", "ashtrays", "accessories", "cutters",
                "t-shirts", "desktop humidor", "humidors", "travel humidor",
                "cigar cases", "humidification", "apparel"},
)

if __name__ == "__main__":
    run(STORE)
