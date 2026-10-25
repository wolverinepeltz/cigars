#!/usr/bin/env python3
"""
cigarsncigars_deal_alert.py - email new deals from cigarsncigars.com.

BigCommerce store; all the scraping logic lives in dealalerts/bigcommerce.py.
This file only holds Cigars N Cigars' settings.

Many listings here come in several pack sizes and show a price range. The
discount is measured on the largest size (top of the range), which is the
size the Was price belongs to; the email notes the cheapest pack.

    python cigarsncigars_deal_alert.py --dry-run
    python cigarsncigars_deal_alert.py --full-catalog
"""

from pathlib import Path

from dealalerts.bigcommerce import BigCommerceStore
from dealalerts.core import run

STORE = BigCommerceStore(
    name="Cigars N Cigars",
    base_url="https://cigarsncigars.com",
    history_file=Path(__file__).parent / "cigarsncigars_sent_history.json",
    core_categories=[
        "cigar-deals",               # "Specials"
        "clearance-premium-cigars",  # "Clearance"
        "deals",
    ],
    full_catalog_category="cigars",  # --full-catalog adds the all-cigars listing
    defaults={"min_discount": 33, "max_price": 125},
)

if __name__ == "__main__":
    run(STORE)
