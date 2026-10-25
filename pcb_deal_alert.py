#!/usr/bin/env python3
"""
pcb_deal_alert.py - email new deals from perfectcigarblend.com.

Perfect Cigar Blend runs on WooCommerce. The page-reading below is specific
to its catalog pages; fetching, deal rules, sent history, email and the
command line are shared (dealalerts/core.py).

Crawls the paginated cigar catalog (/product-category/cigars/page/N/) and
reads each product's price and "You save X%".

    python pcb_deal_alert.py --dry-run
    python pcb_deal_alert.py --min-discount 33 --max-price 125
    python pcb_deal_alert.py --max-pages 5          # quick test
"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dealalerts.core import fetch, run

BASE_URL = "https://perfectcigarblend.com"
CATALOG_URL_TMPL = f"{BASE_URL}/product-category/cigars/page/{{page}}/"
PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
SAVE_RE = re.compile(r"You save(?: up to)?\s*(\d+)%")
PRODUCT_URL_RE = re.compile(r"^https?://perfectcigarblend\.com/product/[^/?#]+/?$")


def get_soup(url, session):
    """Shared fetch (retries, backoff, block detection), parsed as HTML."""
    resp = fetch(url, session)
    if resp is None:
        raise requests.RequestException(f"could not fetch {url}")
    return BeautifulSoup(resp.text, "html.parser")


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Theme-agnostic product extraction.

    Rather than assuming a specific item class (themes vary this a lot),
    this scopes to the WooCommerce product-loop wrapper (`ul.products`,
    a near-universal convention) and then, within it, finds products by
    their permalink pattern (/product/slug/) plus the nearest ancestor
    block that contains a price. This survives theme-specific markup
    changes that would break a hardcoded `li.product` selector.
    """
    scope = soup.select_one("ul.products, .products") or soup

    products = []
    seen_urls = set()

    for a in scope.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not PRODUCT_URL_RE.match(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue  # image-only anchor wrapping the same product link
        if href in seen_urls:
            continue

        # Find the full single-item card by climbing from the title link
        # until climbing further would pull in a SECOND product's link
        # (i.e. we've crossed into a sibling card). This captures the whole
        # item - including badges like "Out of stock" that might sit above
        # or beside the price rather than in the same small wrapper - while
        # still stopping before merging into the next product over.
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
        if "$" not in block_text:
            continue

        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(block_text)]
        if not prices:
            continue
        current_price = min(prices)

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

class PerfectCigarBlendStore:
    name = "Perfect Cigar Blend"
    history_file = Path(__file__).parent / "pcb_sent_history.json"
    defaults = {"min_discount": 40, "max_price": 125, "delay": 1.5}

    def scan(self, session, args):
        items, page = [], 1
        while args.max_pages is None or page <= args.max_pages:
            url = CATALOG_URL_TMPL.format(page=page)
            print(f"Scanning page {page}: {url}")
            try:
                products = _extract_products(get_soup(url, session))
            except requests.RequestException as e:
                print(f"  failed: {e}")
                break
            if not products:
                print("  no products found, stopping.")
                break
            items.extend(products)
            page += 1
            time.sleep(args.delay)
        # A product can drift onto the next page between requests; the
        # shared runner keeps only its first qualifying listing.
        return items, len(items)


STORE = PerfectCigarBlendStore()

if __name__ == "__main__":
    run(STORE)
