#!/usr/bin/env python3
"""
cigarking_deal_alert.py - email new deals from cigarking.com.

BigCommerce store; all the scraping logic lives in dealalerts/bigcommerce.py.
This file only holds Cigar King's settings.

    python cigarking_deal_alert.py --dry-run
    python cigarking_deal_alert.py --min-discount 33 --max-price 125
    python cigarking_deal_alert.py --full-catalog
"""

from pathlib import Path

from dealalerts.bigcommerce import BigCommerceStore
from dealalerts.core import run

STORE = BigCommerceStore(
    name="Cigar King",
    base_url="https://www.cigarking.com",
    history_file=Path(__file__).parent / "cigarking_sent_history.json",
    # Always scanned, on top of deal pages found in the sitemap. The hidden
    # weekend offers page is email-only and missing from the sitemap.
    core_categories=[
        "weekly-deals",
        "slam-packs",
        "weekend-preview-sale-hidden-offers",
    ],
    full_catalog_category=None,  # --full-catalog = every sitemap category
    defaults={"min_discount": 33, "max_price": 125},
)

if __name__ == "__main__":
    run(STORE)
