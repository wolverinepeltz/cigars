#!/usr/bin/env python3
"""
cigarsdaily_deal_alert.py - email new deals from cigarsdaily.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds Cigars Daily's settings.
Each size is its own item.

    python cigarsdaily_deal_alert.py --dry-run
    python cigarsdaily_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="Cigars Daily",
    base_url="https://cigarsdaily.com",
    history_file=Path(__file__).parent / "cigarsdaily_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
