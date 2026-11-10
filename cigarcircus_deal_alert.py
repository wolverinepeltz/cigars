#!/usr/bin/env python3
"""
cigarcircus_deal_alert.py - email new deals from cigarcircus.com.

Cigar Circus runs on Odoo. The page reading below is specific to its shop
grid; fetching, deal rules, sent history, email and the command line are
shared (dealalerts/core.py).

Cigar Circus doesn't show a "was" or MSRP price, so there are two kinds of
deal:

  clearance  anything listed in its clearance categories (CLEARANCE_PAGES)
             at or under --max-price, whatever the discount. Emailed when it
             first shows up, when its price drops, and when it comes back.
  discount   any item in the full catalog showing a struck-through price,
             judged by the usual discount rules. Quiet until the store
             starts showing sale prices.

Listing prices are the cheapest size ("From: $45.60"), usually a 5 pack.

    python cigarcircus_deal_alert.py --dry-run
    python cigarcircus_deal_alert.py --max-pages 1       # quick test
    python cigarcircus_deal_alert.py --no-catalog        # clearance only
"""

import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from dealalerts.core import discount, fetch, run

BASE_URL = "https://www.cigarcircus.com"

# Categories where everything listed counts as a deal.
CLEARANCE_PAGES = [
    "/shop/category/cigars-discounts-clearance-38",
    "/shop/category/while-supplies-last-31",
]
# The full catalog, read only for struck-through (sale) prices.
CATALOG_PAGE = "/shop"

# Product pages look like /shop/partagas-1845-toro-21785; category, cart,
# wishlist and similar pages never end in -<number> right after /shop/.
PRODUCT_PATH_RE = re.compile(r"^/shop/(?!category/|cart|wishlist|checkout|"
                             r"change_pricelist|payment|confirmation|address)"
                             r"[^/?#]+-\d+/?$")
PRICE_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")
SOLD_OUT_WORDS = ("out of stock", "sold out", "not available")


def product_url(href):
    """Canonical product URL (no ?category=...), or None if not a product."""
    full = urljoin(BASE_URL, href)
    parts = urlsplit(full)
    if "cigarcircus.com" not in parts.netloc:
        return None
    if not PRODUCT_PATH_RE.match(parts.path):
        return None
    return f"{BASE_URL}{parts.path.rstrip('/')}"


def page_url(path, page):
    """Odoo paginates as /shop/category/<slug>/page/2."""
    return f"{BASE_URL}{path}" if page == 1 else f"{BASE_URL}{path}/page/{page}"


def get_soup(url, session):
    resp = fetch(url, session)
    if resp is None:
        raise requests.RequestException(f"could not fetch {url}")
    return BeautifulSoup(resp.text, "html.parser")


def _money(el):
    """Price inside an Odoo monetary element, or None."""
    if el is None:
        return None
    val = el.select_one(".oe_currency_value")
    text = val.get_text(strip=True) if val else el.get_text(" ", strip=True)
    m = re.search(r"([\d,]+\.\d{2})", text)
    return float(m.group(1).replace(",", "")) if m else None


def _hidden(el):
    """Odoo leaves the strike-through price in the page but hides it."""
    while el is not None and getattr(el, "attrs", None) is not None:
        classes = el.get("class") or []
        style = (el.get("style") or "").replace(" ", "").lower()
        if "d-none" in classes or "display:none" in style:
            return True
        el = el.parent
    return False


def _card_for(link):
    """
    Climb from a product link to its whole grid card: stop before the level
    that would also contain a different product's link.
    """
    own = product_url(link["href"])
    card = node = link
    for _ in range(12):
        parent = node.parent
        if parent is None or parent.name in ("body", "html"):
            break
        urls = {product_url(a["href"]) for a in parent.find_all("a", href=True)}
        urls.discard(None)
        if urls - {own}:
            break
        card = node = parent
    return card


def _read_card(card):
    """(price, struck-through price or None), or (None, None)."""
    price_box = card.select_one(".product_price") or card

    ref = None
    for d in price_box.select("del, .oe_default_price, .text-decoration-line-through"):
        if not _hidden(d):
            ref = _money(d)
            if ref:
                break

    price = None
    meta = price_box.select_one('[itemprop="price"]')
    if meta is not None:
        raw = meta.get("content") or meta.get_text(strip=True)
        try:
            price = float(str(raw).replace(",", ""))
        except ValueError:
            price = None
    if price is None:
        for el in price_box.select('[data-oe-type="monetary"], .oe_price'):
            if el.find_parent("del") or el.name == "del" or _hidden(el):
                continue
            price = _money(el)
            if price:
                break
    if price is None:
        # Last resort: the lowest dollar amount in the card that isn't the
        # struck-through one.
        amounts = [float(p.replace(",", "")) for p in
                   PRICE_RE.findall(price_box.get_text(" ", strip=True))]
        amounts = [a for a in amounts if ref is None or abs(a - ref) > 0.005]
        price = min(amounts) if amounts else None
    return price, ref


def extract_products(soup, clearance):
    products, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = product_url(a["href"])
        if not url or url in seen:
            continue
        name = a.get_text(" ", strip=True) or a.get("content") or ""
        if not name:
            continue  # image link; the title link for the same card follows
        card = _card_for(a)
        price, ref = _read_card(card)
        if price is None:
            continue
        text = card.get_text(" ", strip=True).lower()
        seen.add(url)
        item = {
            "name": name,
            "url": url,
            "price": price,
            "discount_pct": discount(price, ref),
            "in_stock": not any(w in text for w in SOLD_OUT_WORDS),
        }
        if ref and ref > price:
            item["ref"] = ref
        if clearance:
            item["listed_deal"] = "on clearance"
        products.append(item)
    return products


class CigarCircusStore:
    name = "Cigar Circus"
    history_file = Path(__file__).parent / "cigarcircus_sent_history.json"
    defaults = {"min_discount": 30, "max_price": 150, "delay": 1.5}

    # The whole catalog is roughly 1,500 cigars; a run past this is looping.
    PAGE_CAP = 200

    def add_arguments(self, p):
        p.add_argument("--no-catalog", action="store_true",
                       help="only read the clearance categories, skip the "
                            "full-catalog check for sale prices")

    def _crawl(self, session, args, path, clearance):
        items, seen, page = [], set(), 1
        limit = args.max_pages if args.max_pages is not None else self.PAGE_CAP
        while page <= limit:
            url = page_url(path, page)
            print(f"Scanning {url}")
            try:
                products = extract_products(get_soup(url, session), clearance)
            except requests.RequestException as e:
                print(f"  failed: {e}")
                break
            new = [p for p in products if p["url"] not in seen]
            print(f"  {len(products)} product(s), {len(new)} new")
            if not new:
                # Past the last page Odoo shows the last page again (or none).
                break
            seen.update(p["url"] for p in new)
            items.extend(new)
            page += 1
            time.sleep(args.delay)
        return items

    def scan(self, session, args):
        items = []
        # Clearance first: run() keeps an item's first qualifying listing,
        # so a clearance item found again in the catalog isn't doubled.
        for path in CLEARANCE_PAGES:
            items.extend(self._crawl(session, args, path, clearance=True))
        clearance_count = len(items)
        if not args.no_catalog:
            items.extend(self._crawl(session, args, CATALOG_PAGE, clearance=False))
        on_sale = sum(1 for i in items if i.get("ref"))
        print(f"\n{clearance_count} clearance listing(s); "
              f"{on_sale} listing(s) show a struck-through price.")
        return items, len({i["url"] for i in items})


STORE = CigarCircusStore()

if __name__ == "__main__":
    run(STORE)
