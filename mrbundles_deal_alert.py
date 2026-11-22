#!/usr/bin/env python3
"""
mrbundles_deal_alert.py - email new deals from mrbundles.com.

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds Mr. Bundles's settings. The store lists its own name as every product's vendor, so brands are read
from product tags and names instead. Its markdowns are from its own
"Regular price", so the email labels that price "was".
Each size is its own item.

    python mrbundles_deal_alert.py --dry-run
    python mrbundles_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="Mr. Bundles",
    base_url="https://mrbundles.com",
    history_file=Path(__file__).parent / "mrbundles_sent_history.json",
    # "all" is Shopify's built-in collection of every product.
    collections=["all"],
    defaults={"min_discount": 30, "max_price": 150},
    vendor_is_brand=False,
    ref_label="was",
)

if __name__ == "__main__":
    run(STORE)
