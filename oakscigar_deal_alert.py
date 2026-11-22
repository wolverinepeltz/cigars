#!/usr/bin/env python3
"""
oakscigar_deal_alert.py - email new deals from oakscigar.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds Oaks Cigar's settings. The site also shows an MSRP on some product pages, but that isn't in the
feed; discounts here are from the store's regular price ("was").
Each size is its own item.

    python oakscigar_deal_alert.py --dry-run
    python oakscigar_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="Oaks Cigar",
    base_url="https://www.oakscigar.com",
    history_file=Path(__file__).parent / "oakscigar_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
