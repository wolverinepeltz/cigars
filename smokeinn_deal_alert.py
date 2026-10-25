#!/usr/bin/env python3
"""
smokeinn_deal_alert.py - email new deals from smokeinn.com.

Smoke Inn runs on Miva Merchant. The page-reading below is specific to its
pages; fetching, deal rules, sent history, email and the command line are
shared (dealalerts/core.py).

Scans the Clearance page, where each item prints its retail price, sale
price and "Save X%". With --full-catalog it also scans every brand line
page from the Big List of Brands (~1000 pages), where an MSRP / Save X%
line only appears on discounted sizes.

    python smokeinn_deal_alert.py --dry-run
    python smokeinn_deal_alert.py --min-discount 33 --max-price 125 --full-catalog
    python smokeinn_deal_alert.py --full-catalog --max-lines 20     # quick test
"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dealalerts.core import fetch, run

BASE_URL = "https://www.smokeinn.com"
CLEARANCE_URL = f"{BASE_URL}/Clearance/"
CIGAR_LIST_URL = f"{BASE_URL}/Cigar-List/"
PRODUCT_URL_RE = re.compile(r"^https?://www\.smokeinn\.com/[^/]+\.html$")
RETAIL_RE = re.compile(r"Retail price:\s*\$([\d,]+\.\d{2})\s*\$([\d,]+\.\d{2})")
SAVE_RE = re.compile(r"Save\s*(\d+)%")
SKU_PRICE_RE = re.compile(r"Smoke Inn Price\s*:?\s*\$([\d,]+\.\d{2})")
SKU_RETAIL_SAVE_RE = re.compile(r"Retail Price\s*:?\s*\$([\d,]+\.\d{2})\s*\|\s*Save\s*(\d+)%")
LINE_URL_RE = re.compile(r"^https?://www\.smokeinn\.com/[^/]+/$")
NON_PRODUCT_PATHS = {
    "cigars", "cigar-list", "samplers", "cigar-accessories", "value-bundles",
    "about-us", "smoke-inn-rewards", "smoke-inn-collaboration-samplers",
    "brand-samplers", "showdown-samplers", "all-lighters",
    "lighters-single-flame", "lighters-multi-flame", "lighters-soft-flame",
    "lighters-table-top", "all-cutters", "cutters-guillotine",
    "cutters-punch-cutters", "cutters-v-cutters", "all-ashtrays",
    "ashtrays-1-2-finger", "ashtrays-3-4-finger", "ashtrays-ceramic",
    "ashtrays-metal", "all-cases", "cases-small", "cases-large", "si-gear",
    "the-great-smoke-store-gear", "colibri-brand", "xikar-brand",
    "miscellaneous-accessories", "cotm", "clearance", "special-offers",
    "retail-stores", "contact", "blog", "kma-highlights", "new-to-smokeinn",
    "smoke-inn-exclusive-cigars", "smoke-inn-exclusive-offerings",
    "hot-weekly-cigar-deal", "cigar-of-the-month-club",
    "coupons-and-promotional-restrictions", "privacy-statement",
    "terms-and-conditions", "accessibility-statement",
    "drew-estate-choose-your-swag-promo", "gars-for-gunners",
    "international-orders-1", "return-policy", "age-verification",
    "shipping-rates", "order-tracking",
}


def get_soup(url, session):
    """Shared fetch (retries, backoff, block detection), parsed as HTML."""
    resp = fetch(url, session)
    if resp is None:
        raise requests.RequestException(f"could not fetch {url}")
    return BeautifulSoup(resp.text, "html.parser")


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Finds items by their .html product-link pattern, then climbs from the
    title link to the smallest ancestor block that stays within a single
    item (i.e. climbing further would pull in a second product's link).
    This survives markup differences without needing to guess an exact
    item container class.
    """
    products = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not PRODUCT_URL_RE.match(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue  # image-only anchor wrapping the same product link
        if href in seen_urls:
            continue

        card = a
        node = a
        for _ in range(10):
            if node.parent is None:
                break
            candidate = node.parent
            hrefs_here = {
                urljoin(BASE_URL, x["href"])
                for x in candidate.find_all("a", href=True)
                if PRODUCT_URL_RE.match(urljoin(BASE_URL, x["href"]))
            }
            if len(hrefs_here) > 1:
                break  # climbed too far, this level includes a sibling product
            card = candidate
            node = candidate

        block_text = card.get_text(" ", strip=True)

        retail_match = RETAIL_RE.search(block_text)
        if not retail_match:
            continue  # not a priced item (shouldn't happen on Clearance, but be safe)
        current_price = float(retail_match.group(2).replace(",", ""))

        save_match = SAVE_RE.search(block_text)
        discount_pct = int(save_match.group(1)) if save_match else 0

        out_of_stock = "out of stock" in block_text.lower()

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": current_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products


def _extract_line_page_products(soup: BeautifulSoup) -> list[dict]:
    """
    Parses the 'Product / Price / MSRP / Qty / Cart' table used on line
    pages (e.g. /Padron-1926-No-47/) and the Hot Weekly Deal page. Unlike
    Clearance, MSRP/Save% is OPTIONAL here - it only appears when a SKU has
    an active discount; a row with no MSRP text just means full price, so
    it gets discount_pct=0 and is filtered out downstream like any other
    non-qualifying item.
    """
    products = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not PRODUCT_URL_RE.match(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        if href in seen_urls:
            continue

        card = a
        node = a
        for _ in range(10):
            if node.parent is None:
                break
            candidate = node.parent
            hrefs_here = {
                urljoin(BASE_URL, x["href"])
                for x in candidate.find_all("a", href=True)
                if PRODUCT_URL_RE.match(urljoin(BASE_URL, x["href"]))
            }
            if len(hrefs_here) > 1:
                break
            card = candidate
            node = candidate

        block_text = card.get_text(" ", strip=True)

        price_match = SKU_PRICE_RE.search(block_text)
        if not price_match:
            continue  # not a priced row (e.g. picked up a stray nav link)
        current_price = float(price_match.group(1).replace(",", ""))

        retail_save_match = SKU_RETAIL_SAVE_RE.search(block_text)
        discount_pct = int(retail_save_match.group(2)) if retail_save_match else 0

        out_of_stock = "out of stock" in block_text.lower()

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": current_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products


def scrape_brand_lines(session: requests.Session, max_lines: int | None = None) -> list[str]:
    """Returns line-page URLs found on the Big List of Brands page."""
    print(f"Fetching brand/line list: {CIGAR_LIST_URL}")
    try:
        soup = get_soup(CIGAR_LIST_URL, session)
    except requests.RequestException as e:
        print(f"  failed: {e}")
        return []

    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not LINE_URL_RE.match(href):
            continue
        path_segment = href.rstrip("/").rsplit("/", 1)[-1].lower()
        if path_segment in NON_PRODUCT_PATHS:
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
        if max_lines is not None and len(urls) >= max_lines:
            break

    print(f"  found {len(urls)} candidate line page(s)")
    return urls

class SmokeInnStore:
    name = "Smoke Inn"
    history_file = Path(__file__).parent / "smokeinn_sent_history.json"
    defaults = {"min_discount": 40, "max_price": 125, "delay": 1.0}

    def add_arguments(self, p):
        p.add_argument("--url", default=CLEARANCE_URL,
                       help="override the Clearance page to scan")
        p.add_argument("--full-catalog", action="store_true",
                       help="also crawl every brand/line page from the Big List "
                            "of Brands (~1000 pages, much slower)")
        p.add_argument("--max-lines", type=int, default=None,
                       help="full-catalog mode: cap how many line pages to scan")

    def scan(self, session, args):
        print(f"Scanning: {args.url}")
        items = []
        try:
            items = _extract_products(get_soup(args.url, session))
            print(f"  found {len(items)} priced item(s) on the page")
        except requests.RequestException as e:
            print(f"  failed: {e}")

        if args.full_catalog:
            print("\nStarting full-catalog crawl (this takes a while) ...")
            line_urls = scrape_brand_lines(session, max_lines=args.max_lines)
            for i, url in enumerate(line_urls, 1):
                if i % 50 == 0 or i == len(line_urls):
                    print(f"  ... {i}/{len(line_urls)} line pages scanned")
                try:
                    items.extend(_extract_line_page_products(get_soup(url, session)))
                except requests.RequestException as e:
                    print(f"  {url} failed: {e}")
                time.sleep(args.delay)
        # Clearance items come first, so a product on both keeps its
        # Clearance listing (the shared runner keeps the first match).
        return items, len(items)


STORE = SmokeInnStore()

if __name__ == "__main__":
    run(STORE)
