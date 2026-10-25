#!/usr/bin/env python3
"""
cigora_deal_alert.py - email new deals from cigora.com.

Cigora runs on Salesforce Commerce Cloud (no public product API). The
page-reading below is specific to its category listings (24 per page,
?start=N&sz=24); fetching, deal rules, sent history, email and the command
line are shared (dealalerts/core.py).

Cigora bot-protects aggressively: requests from some IPs (including GitHub
Actions runners) get HTTP 403, while the same code often works from a home
connection.

    python cigora_deal_alert.py --dry-run
    python cigora_deal_alert.py --min-discount 33.333 --max-price 125
    python cigora_deal_alert.py --category samplers
    python cigora_deal_alert.py --max-pages 5        # quick test
"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dealalerts.core import fetch, run

BASE_URL = "https://www.cigora.com"
CATEGORY = "cigars"                      # /category/<name>/
PAGE_SIZE = 24                           # products per listing page
LISTING_URL_TMPL = (BASE_URL + "/category/{category}/"
                   "?start={start}&sz=" + str(PAGE_SIZE))
PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
PRODUCT_URL_RE = re.compile(r"^https?://(?:www\.)?cigora\.com/product/[^?#]+\.html")
OUT_OF_STOCK_RES = [
    re.compile(r"\bout of stock\b", re.I),
    re.compile(r"\bsold out\b", re.I),
    re.compile(r"\bnot available\b", re.I),
]


def get_soup(url, session):
    """Shared fetch (retries, backoff, block detection), parsed as HTML."""
    resp = fetch(url, session)
    if resp is None:
        raise requests.RequestException(f"could not fetch {url}")
    return BeautifulSoup(resp.text, "html.parser")


def _card_prices(card) -> tuple[float | None, float | None]:
    """
    Returns (sale_price, list_price).

    Cigora's tile markup carries exact numeric values in `content`
    attributes:
      sale: <span class="sales"><span class="value" content="49.99">
      list: <del><span class="strike-through list"><span class="value"
             content="56.99">
    Falls back to SFCC-generic selectors, then to min/max of every
    $ amount in the card.
    """
    def content_of(selector):
        el = card.select_one(selector)
        if el and el.has_attr("content"):
            try:
                return float(el["content"])
            except (TypeError, ValueError):
                pass
        return None

    sale = (content_of(".sales .value")
            or content_of(".price .sales .value"))
    list_p = (content_of(".strike-through .value")
              or content_of("del .value")
              or content_of(".list .value"))

    if sale is None or list_p is None:
        # Fallback: every $ amount in the card.
        amounts = [float(p.replace(",", ""))
                   for p in PRICE_RE.findall(card.get_text(" ", strip=True))]
        if amounts:
            lo, hi = min(amounts), max(amounts)
            sale = sale if sale is not None else lo
            if list_p is None and hi > sale:
                list_p = hi
    return sale, list_p


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Extracts one record per div.product tile.

    Tile structure (verified against the live site):
      div.product[data-pid]                      <- one tile
        a[href=/product/<slug>/<id>.html]        <- product link
        span.product-tile-product-name           <- name
        span.product-tile-badges                 <- "Sale" badge (no % given)
        .sales .value[content]                   <- sale price
        del .strike-through .value[content]      <- list price
      "SOLD OUT" text marks unavailable tiles.
    """
    products = []
    seen_urls = set()

    for tile in soup.select("div.product"):
        link = tile.select_one("a[href]")
        if not link:
            continue
        href = urljoin(BASE_URL, link["href"]).split("?")[0].split("#")[0]
        if not PRODUCT_URL_RE.match(href) or href in seen_urls:
            continue

        name_el = tile.select_one(".product-tile-product-name")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        sale_price, list_price = _card_prices(tile)
        if sale_price is None:
            continue

        if list_price and list_price > sale_price:
            discount_pct = round((list_price - sale_price) / list_price * 100, 1)
        else:
            discount_pct = 0

        tile_text = tile.get_text(" ", strip=True)
        out_of_stock = any(rx.search(tile_text) for rx in OUT_OF_STOCK_RES)

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": sale_price,
            "list_price": list_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products

class CigoraStore:
    name = "Cigora"
    history_file = Path(__file__).parent / "cigora_sent_history.json"
    defaults = {"min_discount": 40, "max_price": 125, "delay": 1.5}

    def add_arguments(self, p):
        p.add_argument("--category", default=CATEGORY,
                       help="category slug under /category/ (default: cigars)")

    def scan(self, session, args):
        items, seen, page = [], set(), 0
        while args.max_pages is None or page < args.max_pages:
            start = page * PAGE_SIZE
            url = LISTING_URL_TMPL.format(category=args.category, start=start)
            print(f"Scanning page {page + 1} (start={start}): {url}")
            try:
                products = _extract_products(get_soup(url, session))
            except requests.RequestException as e:
                print(f"  failed: {e}")
                break
            if not products:
                print("  no products found, stopping.")
                break
            for p in products:
                if p["url"] not in seen:  # dedup across pages, first listing wins
                    seen.add(p["url"])
                    p["ref"], p["ref_label"] = p.get("list_price"), "was"
                    items.append(p)
            if len(products) < PAGE_SIZE:  # SFCC listings end on a short page
                print("  short page - end of catalog.")
                break
            page += 1
            time.sleep(args.delay)
        return items, len(items)


STORE = CigoraStore()

if __name__ == "__main__":
    run(STORE)
