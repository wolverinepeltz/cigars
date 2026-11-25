#!/usr/bin/env python3
"""
foxcigar_deal_alert.py - email new deals from foxcigar.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds Fox Cigar's settings.
Each size is its own item.

    python foxcigar_deal_alert.py --dry-run
    python foxcigar_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="Fox Cigar",
    base_url="https://foxcigar.com",
    history_file=Path(__file__).parent / "foxcigar_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
