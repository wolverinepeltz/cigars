"""
WooCommerce stores: Cigar Country, NH Cigars, Oaks Cigar.

WooCommerce sites publish their catalog through the public Store API (the
same feed the site's own cart and product blocks use):

    /wp-json/wc/store/v1/products?type=simple&per_page=100&page=N
    /wp-json/wc/store/v1/products?type=variation&per_page=100&page=N

Each entry carries exact prices in cents:
    prices.price          what the store charges now
    prices.regular_price  the price it's marked down from
    is_in_stock           in stock or not
Nothing is parsed out of page text, so theme changes can't break it.

Simple products are one item each. Products that come in several sizes
(single, 5-pack, box) are "variable": each size is a separate "variation"
with its own price and stock, read from the variation listing, and its
link opens the product page with that size picked. The variable products
themselves are read only for their categories, so accessories can be left
out.
"""

import html
import re
import time

from .core import discount, fetch

API = "/wp-json/wc/store/v1/products"
PAGE_SIZE = 100  # the Store API's maximum

# Category slugs (or words in them) that are never cigars.
SKIP_CATEGORY_WORDS = {
    "accessories", "accessory", "humidor", "humidors", "lighter", "lighters",
    "cutter", "cutters", "ashtray", "ashtrays", "apparel", "nicotine",
    "pouches", "pipe", "pipes", "gift-card", "gift-cards", "merchandise",
}
NON_CIGAR_NAME_RE = re.compile(
    r"\b(humidors?|lighters?|cutters?|ashtrays?|travel case|cigar case|"
    r"nicotine|pouches|gift card|boveda|hygrometer|pipe tobacco|t-shirt)\b",
    re.I)
# "Pack Size: Box of 25" -> "Box of 25"; "Flavor: Mint, Strength: 4mg" -> as is.
LABEL_RE = re.compile(r"^[^:,]{1,30}:\s*")


def clean(text):
    """Decode HTML entities (&#8243; -> ″) and squeeze spaces."""
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def cents(prices, key):
    """Store API prices are strings in the currency's minor unit ("4795")."""
    raw = (prices or {}).get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw) / 10 ** int(prices.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        return None


def size_label(variation_text):
    """The variation's option, without the attribute name in front."""
    text = clean(variation_text)
    if text.count(":") == 1:
        return LABEL_RE.sub("", text)
    return text


def category_slugs(product):
    return [c.get("slug", "") for c in product.get("categories") or []]


def is_accessory(slugs):
    for slug in slugs:
        words = set(slug.lower().split("-"))
        if slug.lower() in SKIP_CATEGORY_WORDS or words & SKIP_CATEGORY_WORDS:
            return True
    return False


def brand_of(product):
    brands = product.get("brands") or []
    return clean(brands[0].get("name")) if brands else None


def to_item(product, ref_label, brand=None):
    """One item from a simple product or a variation; None if unpriced."""
    prices = product.get("prices") or {}
    price = cents(prices, "price")
    if price is None or price <= 0:
        return None
    regular = cents(prices, "regular_price")
    ref = regular if regular and regular > price else None
    return {
        "name": clean(product.get("name")),
        "brand": brand_of(product) or brand,
        "size": size_label(product.get("variation")),
        "price": price,
        "ref": ref,
        "ref_label": ref_label,
        "discount_pct": discount(price, ref),
        "in_stock": bool(product.get("is_in_stock")) and product.get("is_purchasable", True),
        "url": product.get("permalink") or "",
        "product_id": product.get("id"),
    }


class WooCommerceStore:
    """
    name          shown in logs and email
    base_url      e.g. "https://nhcigars.com"
    history_file  Path to <store>_sent_history.json
    defaults      default CLI values, e.g. {"min_discount": 30}
    ref_label     what to call regular_price in the email: "was" (the
                  store's own regular price) or "MSRP"
    """

    def __init__(self, name, base_url, history_file, defaults=None, ref_label="was"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.history_file = history_file
        self.defaults = defaults or {}
        self.ref_label = ref_label

    def listing(self, session, kind, max_pages, delay):
        """Every product of one type ("simple", "variable", "variation")."""
        page = 1
        while max_pages is None or page <= max_pages:
            url = f"{self.base_url}{API}"
            params = {"type": kind, "per_page": PAGE_SIZE, "page": page}
            print(f"Reading {kind} products, page {page}")
            resp = fetch(url, session, params=params, accept="application/json")
            if resp is None:
                return
            try:
                batch = resp.json()
            except ValueError:
                print("  response wasn't JSON (a bot-check page, maybe)")
                return
            if not isinstance(batch, list) or not batch:
                return
            yield from batch
            total_pages = resp.headers.get("x-wp-totalpages")
            if len(batch) < PAGE_SIZE or (total_pages and page >= int(total_pages)):
                return
            page += 1
            time.sleep(delay)

    def scan(self, session, args):
        items, skipped = [], 0

        for p in self.listing(session, "simple", args.max_pages, args.delay):
            if is_accessory(category_slugs(p)) or NON_CIGAR_NAME_RE.search(clean(p.get("name"))):
                skipped += 1
                continue
            item = to_item(p, self.ref_label)
            if item:
                items.append(item)
        time.sleep(args.delay)

        # Parents of the sizes: categories (to drop accessories) and brand.
        parents = {}
        for p in self.listing(session, "variable", args.max_pages, args.delay):
            parents[p.get("id")] = p
        time.sleep(args.delay)

        for v in self.listing(session, "variation", args.max_pages, args.delay):
            parent = parents.get(v.get("parent"), {})
            name = clean(v.get("name") or parent.get("name"))
            if is_accessory(category_slugs(parent)) or NON_CIGAR_NAME_RE.search(name):
                skipped += 1
                continue
            item = to_item(v, self.ref_label, brand=brand_of(parent))
            if item:
                items.append(item)

        if skipped:
            print(f"  skipped {skipped} accessory/non-cigar item(s)")
        return items, len(items)
