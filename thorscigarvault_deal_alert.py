#!/usr/bin/env python3
"""
thorscigarvault_deal_alert.py - email new deals from thorscigarvault.com.

WooCommerce store; all the logic lives in dealalerts/woocommerce.py. This
file only holds Thor's Cigar Vault's settings.
Each size is its own item.

    python thorscigarvault_deal_alert.py --dry-run
    python thorscigarvault_deal_alert.py --dry-run --max-pages 1      # quick test
"""

from pathlib import Path

from dealalerts.core import run
from dealalerts.woocommerce import WooCommerceStore

STORE = WooCommerceStore(
    name="Thor's Cigar Vault",
    base_url="https://thorscigarvault.com",
    history_file=Path(__file__).parent / "thorscigarvault_sent_history.json",
    defaults={"min_discount": 30, "max_price": 150},
)

if __name__ == "__main__":
    run(STORE)
