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
    python cigora_deal_alert.py --min-discount 30 --max-price 150
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
    Returns (sale_price, list_price), or (None, None) if the tile can't be
    priced as a deal.

    Cigora's tiles carry exact numbers in `content` attributes. Two layouts
    (checked against the live site, Nov 2026):

      single price:  .strike-through .value[content]   list (MSRP) price
                     .sales .value[content]            sale price
      price range:   .price .range  "As low as $X - $Y"  (several sizes)
                     a struck price, when present, sits right before the
                     .sales.promo-price it applies to: "$39.99, was $126.99"

    A range with no struck price has no markdown to measure (its low and
    high ends are different pack sizes), so it's skipped rather than read
    as "the single cigar is 95% off the box price".
    """
    def content_of(el):
        if el is not None and el.has_attr("content"):
            try:
                return float(el["content"])
            except (TypeError, ValueError):
                pass
        return None

    price = card.select_one(".price")
    struck = content_of(card.select_one(".strike-through .value"))

    if price is not None and price.select_one(".range") is not None:
        if struck is None:
            return None, None
        promo = (card.select_one(".sales.promo-price .value")
                 or card.select_one(".range .sales .value"))
        return content_of(promo), struck

    sale = content_of(card.select_one(".sales .value"))
    if sale is None and price is None:
        # No price markup at all: fall back to the tile's $ amounts, but
        # only when there are exactly two (was / now).
        amounts = [float(a.replace(",", ""))
                   for a in PRICE_RE.findall(card.get_text(" ", strip=True))]
        if len(amounts) == 2:
            return min(amounts), max(amounts)
        return None, None
    return sale, struck


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Extracts one record per div.product tile.

    Tile structure (checked against the live site, Nov 2026):
      div.product[data-pid]                      <- one tile
        a.append-wishlist-modal[href=""]         <- "Add to Favorites"
        a[href=/product/<slug>/<id>.html]        <- product link
        p.product-tile-product-name              <- name
        span.product-tile-badge                  <- "Sale" badge (no % given)
        .sales .value[content]                   <- sale price
        del .strike-through .value[content]      <- list price
      "SOLD OUT" text marks unavailable tiles.
    """
    products = []
    seen_urls = set()

    for tile in soup.select("div.product"):
        # The first link in a tile is now the "Add to Favorites" button
        # (empty href), so pick the product link explicitly.
        link = tile.select_one('a[href*="/product/"]')
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
    defaults = {"min_discount": 30, "max_price": 150, "delay": 1.5}

    def add_arguments(self, p):
        p.add_argument("--category", default=CATEGORY,
                       help="category slug under /category/ (default: cigars)")

    # Safety cap; a run that reaches it is looping.
    PAGE_CAP = 150

    def scan(self, session, args):
        items, seen, seen_tiles, page = [], set(), set(), 0
        tiles_read = 0
        limit = args.max_pages if args.max_pages is not None else self.PAGE_CAP
        while page < limit:
            start = page * PAGE_SIZE
            url = LISTING_URL_TMPL.format(category=args.category, start=start)
            print(f"Scanning page {page + 1} (start={start}): {url}")
            try:
                soup = get_soup(url, session)
            except requests.RequestException as e:
                print(f"  failed: {e}")
                break
            # Count the page's product tiles, not just the ones that could be
            # priced as deals (most are price ranges with no markdown): the
            # end of the catalog is a page with fewer than PAGE_SIZE tiles.
            tile_ids = [t.get("data-pid") or str(i)
                        for i, t in enumerate(soup.select("div.product[data-pid]"))]
            if not tile_ids:
                print("  no products on this page, stopping.")
                break
            if not set(tile_ids) - seen_tiles:
                print("  nothing new on this page (site is repeating pages), stopping.")
                break
            seen_tiles.update(tile_ids)
            tiles_read += len(tile_ids)
            for p in _extract_products(soup):
                if p["url"] not in seen:  # dedup across pages, first listing wins
                    seen.add(p["url"])
                    p["ref"], p["ref_label"] = p.get("list_price"), "was"
                    items.append(p)
            if len(tile_ids) < PAGE_SIZE:  # SFCC listings end on a short page
                print("  short page - end of catalog.")
                break
            page += 1
            time.sleep(args.delay)
        else:
            if args.max_pages is None:
                print(f"  stopped at the {self.PAGE_CAP}-page safety cap.")
        print(f"  {tiles_read} product(s) listed, {len(items)} with a price "
              f"that can be compared (the rest are ranges with no markdown)")
        # "examined" counts every listed product, so a catalog of mostly
        # unpriceable ranges isn't mistaken for "nothing was read".
        return items, tiles_read


STORE = CigoraStore()

if __name__ == "__main__":
    run(STORE)
