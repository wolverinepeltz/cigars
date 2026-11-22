#!/usr/bin/env python3
"""
cigarcountry_deal_alert.py - email new deals from cigarcountry.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds Cigar Country's settings. Cigar Country ships from the Dominican Republic. Almost every product comes
in several sizes, so a run reads about 90 pages.
Each size is its own item.

    python cigarcountry_deal_alert.py --dry-run
    python cigarcountry_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="Cigar Country",
    base_url="https://cigarcountry.com",
    history_file=Path(__file__).parent / "cigarcountry_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
