#!/usr/bin/env python3
"""
nhcigars_deal_alert.py - email new deals from nhcigars.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds NH Cigars's settings. Nearly every product is marked down from a list price, which the email
labels "was". Nicotine pouches and accessories are left out.
Each size is its own item.

    python nhcigars_deal_alert.py --dry-run
    python nhcigars_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="NH Cigars",
    base_url="https://nhcigars.com",
    history_file=Path(__file__).parent / "nhcigars_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
