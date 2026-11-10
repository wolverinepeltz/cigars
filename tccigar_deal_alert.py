#!/usr/bin/env python3
"""
tccigar_deal_alert.py - email new deals from tccigar.com (Treasure Coast Cigars).

Shopify store; all the logic lives in dealalerts/shopify.py. This file only
holds TC Cigar's settings. The store marks prices down from its own
"Regular price", so the email labels that price "was" rather than "MSRP".
Each size (single, box) is its own item.

    python tccigar_deal_alert.py --dry-run
    python tccigar_deal_alert.py --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.shopify import ShopifyStore

STORE = ShopifyStore(
    name="TC Cigar",
    base_url="https://www.tccigar.com",
    history_file=Path(__file__).parent / "tccigar_sent_history.json",
    # "all" is the whole catalog (about 1,065 products, cigars and a few
    # accessories); value bundles are added in case one isn't filed under it.
    # Duplicates are removed.
    collections=["all", "buy-cigar-bundles-value-brand-cigars"],
    defaults={"min_discount": 30, "max_price": 150},
    ref_label="was",
)

if __name__ == "__main__":
    run(STORE)
