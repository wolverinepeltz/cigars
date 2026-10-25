"""
Shopify stores: GT Cigars.

Shopify publishes a public feed for every collection:

    /collections/<handle>/products.json?limit=250&page=N

with exact numbers for every size (variant) of every product:
    price             what the store charges now
    compare_at_price  the MSRP it's marked down from
    available         in stock or not
Nothing is parsed out of page text, so markup changes can't break it.

Each size is its own item, and its link opens the product page with that
size pre-selected (?variant=<id>).
"""

import time

from .core import discount, fetch

PAGE_SIZE = 250  # Shopify's max per page for products.json


def to_float(raw):
    """Shopify sends prices as strings like "79.99", or null."""
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def variant_items(product, base_url):
    """One item per size of one product."""
    handle = product.get("handle", "")
    name = (product.get("title") or "").strip()
    items = []
    for v in product.get("variants") or []:
        price = to_float(v.get("price"))
        if price is None:
            continue
        msrp = to_float(v.get("compare_at_price"))
        # compare_at below the price shows up in some stores' data (a stale
        # MSRP field); discount() treats that as no discount.
        size = (v.get("title") or "").strip()
        if size.lower() == "default title":  # Shopify's name for "no options"
            size = ""
        items.append({
            "name": name,
            "size": size,
            "price": price,
            "ref": msrp if msrp and msrp > price else None,
            "ref_label": "MSRP",
            "discount_pct": discount(price, msrp),
            "in_stock": bool(v.get("available")),
            "variant_id": v.get("id"),
            "url": f"{base_url}/products/{handle}?variant={v.get('id')}",
        })
    return items


class ShopifyStore:
    """
    name          shown in logs and email
    base_url      e.g. "https://gtcigars.com"
    history_file  Path to <store>_sent_history.json
    collections   collection handles scanned by default
    defaults      default CLI values, e.g. {"min_discount": 35}
    """

    def __init__(self, name, base_url, history_file, collections, defaults=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.history_file = history_file
        self.collections = collections
        self.defaults = defaults or {}

    def add_arguments(self, p):
        p.add_argument("--collections", nargs="+", default=self.collections,
                       help=f"collection handles to scan (default: "
                            f"{' '.join(self.collections)})")

    def scrape_collection(self, session, handle, max_pages, delay):
        url = f"{self.base_url}/collections/{handle}/products.json"
        page = 1
        while max_pages is None or page <= max_pages:
            print(f"Scanning {handle} page {page}: {url}?limit={PAGE_SIZE}&page={page}")
            resp = fetch(url, session, params={"limit": PAGE_SIZE, "page": page},
                         accept="application/json")
            if resp is None:
                return
            try:
                products = resp.json().get("products", [])
            except ValueError:
                print("  response wasn't JSON (a bot-check page, maybe)")
                return
            if not products:
                print("  no more products.")
                return
            print(f"  {len(products)} product(s)")
            for p in products:
                yield from variant_items(p, self.base_url)
            if len(products) < PAGE_SIZE:
                return  # short page means that was the last one
            page += 1
            time.sleep(delay)

    def scan(self, session, args):
        seen, items = set(), []
        for n, handle in enumerate(args.collections):
            if n:
                time.sleep(args.delay)
            for item in self.scrape_collection(session, handle, args.max_pages,
                                               args.delay):
                if item["variant_id"] in seen:
                    continue  # same item listed in more than one collection
                seen.add(item["variant_id"])
                items.append(item)
        return items, len(items)
