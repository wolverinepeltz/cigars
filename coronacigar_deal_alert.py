#!/usr/bin/env python3
"""
coronacigar_deal_alert.py - email new deals from coronacigar.com.

BigCommerce store; all the scraping logic lives in dealalerts/bigcommerce.py.
This file only holds Corona Cigar's settings.

Corona discounts against MSRP, and on multi-size listings both MSRP and the
price are ranges ("MSRP $90.00 - $200.00", "$45.95 - $119.95"); the
discount is measured on the top of each, the box.

    python coronacigar_deal_alert.py --dry-run
    python coronacigar_deal_alert.py --full-catalog
"""

from pathlib import Path

from dealalerts.bigcommerce import BigCommerceStore
from dealalerts.core import run

STORE = BigCommerceStore(
    name="Corona Cigar",
    base_url="https://www.coronacigar.com",
    history_file=Path(__file__).parent / "coronacigar_sent_history.json",
    core_categories=[
        "cigar-deals",
        "cigars/categories/clearance-cigars",
        "cigars/cigar-fan-favorites/cigar-deals",
    ],
    full_catalog_category=None,  # --full-catalog = every sitemap category
    defaults={"min_discount": 33, "max_price": 125},
)

if __name__ == "__main__":
    run(STORE)
