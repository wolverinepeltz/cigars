"""
BigCommerce (Stencil theme) stores: Cigar King, Cigars N Cigars, Corona Cigar.

BigCommerce has no public product feed, so this reads the product tiles on
category listing pages (?page=N). Each tile carries up to three prices,
marked with data attributes even when the theme hides the labels:

    MSRP   [data-product-rrp-price-without-tax]
    Was    [data-product-non-sale-price-without-tax]
    Now    [data-product-price-without-tax]

Discount is Was -> Now, falling back to MSRP -> Now. If those attributes
are missing it reads the dollar amounts in the tile instead (last = Now,
highest earlier one = reference).

Multi-size listings show prices as ranges, e.g. Now "$31.95 - $94.95".
The reference price on those belongs to the default size, the top of the
range (usually the box), so the discount is worked out on the top price.
A range with nothing to compare against is skipped.

Deal pages: a fixed list per store (core_categories), plus any category in
the store's sitemap whose URL contains a deal word (sale, clearance, % off,
...). Stores create new promo pages all the time, so they're found fresh
on every run.
"""

import html
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .core import discount, fetch

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d{2})?)\s*[-–]\s*\$\s*([\d,]+(?:\.\d{2})?)")
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
OUT_OF_STOCK_RE = re.compile(r"\b(out of stock|sold out)\b", re.I)

# A category URL containing any of these words counts as a deal page.
# ("red" is left out on purpose: it matches lines like Cohiba Red Dot.)
DEAL_WORDS = {
    "deal", "deals", "sale", "clearance", "special", "specials", "off",
    "slam", "closeout", "blowout", "discount", "steal", "steals", "overstock",
}
# Product-line names that contain a deal word but aren't promos.
NOT_DEAL_PHRASES = ("special-selection", "special-edition", "special-reserve")
# Category URLs never worth scanning for cigar deals.
SKIP_CATEGORY_WORDS = {
    "humidor", "humidors", "accessories", "accessory", "lighter", "lighters",
    "cutter", "cutters", "ashtray", "ashtrays", "gift", "apparel", "case",
    "cases", "lifestyle",
}
# Product names that aren't cigars (clearance pages mix these in).
NON_CIGAR_NAME_RE = re.compile(
    r"\b(humidors?|lighters?|cutters?|ashtrays?|travel case|cigar case)\b", re.I)


# --------------------------------------------------------------------------
# Price parsing
# --------------------------------------------------------------------------

def money(text):
    """'$118.75' -> 118.75. A range returns its top price. Empty -> None."""
    if not text:
        return None
    rng = price_range(text)
    if rng:
        return rng[1]
    m = PRICE_RE.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def price_range(text):
    """'$31.95 - $94.95' -> (31.95, 94.95), or None if not a range."""
    m = RANGE_RE.search(text or "")
    if not m:
        return None
    lo, hi = (float(x.replace(",", "")) for x in m.groups())
    return (min(lo, hi), max(lo, hi))


def card_prices(card):
    """
    Returns (now, ref, ref_label, from_price):
      now         price to compare (top of the range on multi-size tiles)
      ref         Was or MSRP price above it, or None
      ref_label   "was" or "MSRP"
      from_price  bottom of the range on multi-size tiles, else None
    now is None when the tile can't be priced.
    """
    def pick(*selectors):
        for sel in selectors:
            el = card.select_one(sel)
            if el is not None:
                return el.get_text(" ", strip=True)
        return None

    now_txt = pick("[data-product-price-without-tax]", ".price--withoutTax",
                   ".price--main")
    was_txt = pick("[data-product-non-sale-price-without-tax]", ".price--non-sale")
    rrp_txt = pick("[data-product-rrp-price-without-tax]", ".price--rrp")

    if now_txt is not None:
        rng = price_range(now_txt)
        now = rng[1] if rng else money(now_txt)
        from_price = rng[0] if rng else None
        if now is None:
            return None, None, None, None
        was, rrp = money(was_txt), money(rrp_txt)
        if was and was > now:
            ref, label = was, "was"
        elif rrp and rrp > now:
            ref, label = rrp, "MSRP"
        else:
            ref, label = None, None
        if from_price is not None and ref is None:
            return None, None, None, None  # sizes vary, nothing to compare to
        return now, ref, label, from_price

    # Fallback: no recognisable price markup, read the dollar amounts.
    text = card.get_text(" ", strip=True)
    rng = price_range(text)
    amounts = [float(a.replace(",", "")) for a in PRICE_RE.findall(text)]
    if rng:
        lo, hi = rng
        higher = [a for a in amounts if a > hi]
        if not higher:
            return None, None, None, None
        return hi, max(higher), "was", lo
    if not amounts:
        return None, None, None, None
    now = amounts[-1]
    earlier = [a for a in amounts[:-1] if a > now]
    return now, (max(earlier) if earlier else None), "was", None


def find_cards(soup):
    """Product tiles. Stencil marks them with data-entity-id."""
    cards = soup.select("article.card[data-entity-id], [data-entity-id].card")
    if not cards:
        cards = soup.select("article.card, li.product, .productGrid .product")
    return cards


# Link/button text that is never a product name.
BUTTON_TEXT_RE = re.compile(
    r"^(choose options|add to cart|quick view|view details?|compare|"
    r"out of stock|sold out|pre-?order|buy now|shop now|learn more|sale|new)$", re.I)
# Stores tack these onto image descriptions: "X Cigars For Sale".
ALT_SUFFIX_RE = re.compile(r"\s+(cigars?\s+for\s+sale|for\s+sale)\s*$", re.I)


def card_name(card, link):
    """
    The product name on a tile. Themes differ on where it lives, so try, in
    order: the title/heading text, the product image's description, then
    link text that isn't a button label, then the web address.
    """
    def clean(text):
        return re.sub(r"\s+", " ", text or "").strip()

    for sel in (".card-title", "[data-test='card-title']", ".product-title",
                ".productView-title", "h2", "h3", "h4", "h5"):
        el = card.select_one(sel)
        if el is not None:
            text = clean(el.get_text(" ", strip=True))
            if text and not BUTTON_TEXT_RE.match(text):
                return text
    for attr in ("data-product-name", "data-name", "aria-label", "title"):
        for el in card.select(f"[{attr}]"):
            text = clean(el.get(attr))
            if text and not BUTTON_TEXT_RE.match(text) and not text.lower().startswith(
                    ("quick view", "compare", "add ")):
                return ALT_SUFFIX_RE.sub("", text)
    img = card.select_one("img[alt]")
    if img is not None and clean(img["alt"]):
        return ALT_SUFFIX_RE.sub("", clean(img["alt"]))
    for a in card.select("a[href]"):
        text = clean(a.get_text(" ", strip=True))
        if text and not BUTTON_TEXT_RE.match(text):
            return text
    slug = urlparse(link["href"]).path.strip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").title()


def product_link(card):
    """The tile's link to the product page (not add-to-cart or compare)."""
    link = card.select_one(".card-title a[href], h3 a[href], h4 a[href]")
    if link is not None:
        return link
    return next((a for a in card.select("a[href]")
                 if "cart.php" not in a["href"] and "compare" not in a["href"]
                 and not a["href"].startswith(("#", "javascript"))), None)


def extract_products(page_html, base_url):
    """Returns (items, tiles skipped as accessories or unpriceable)."""
    soup = BeautifulSoup(page_html, "html.parser")
    items, skipped = [], 0
    for card in find_cards(soup):
        link = product_link(card)
        if link is None:
            continue
        name = card_name(card, link)
        if not name:
            continue
        if NON_CIGAR_NAME_RE.search(name):
            skipped += 1
            continue
        now, ref, label, from_price = card_prices(card)
        if now is None:
            skipped += 1
            continue
        items.append({
            "name": name,
            "url": urljoin(base_url, link["href"]).split("?")[0].split("#")[0],
            "price": now,
            "ref": ref,
            "ref_label": label,
            "from_price": from_price,
            "discount_pct": discount(now, ref),
            "in_stock": not OUT_OF_STOCK_RE.search(card.get_text(" ", strip=True)),
        })
    return items, skipped


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def path_of(url):
    return urlparse(url).path.strip("/").lower()


class BigCommerceStore:
    """
    name                   shown in logs and email
    base_url               e.g. "https://www.cigarking.com"
    history_file           Path to <store>_sent_history.json
    core_categories        category paths always scanned, e.g. ["weekly-deals"]
    full_catalog_category  path scanned by --full-catalog; None means every
                           category in the sitemap
    defaults               default CLI values, e.g. {"min_discount": 33}
    """

    def __init__(self, name, base_url, history_file, core_categories,
                 full_catalog_category=None, defaults=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.history_file = history_file
        self.core_categories = core_categories
        self.full_catalog_category = full_catalog_category
        self.defaults = defaults or {}

    def url_for(self, path):
        return f"{self.base_url}/{path.strip('/')}/"

    # -- CLI ---------------------------------------------------------------
    def add_arguments(self, p):
        p.add_argument("--categories", nargs="+", default=None,
                       help="scan only these category paths, e.g. "
                            f"{self.core_categories[0]} (default: deal pages)")
        p.add_argument("--full-catalog", action="store_true",
                       help="also scan the whole catalog (much slower)")

    # -- which pages -------------------------------------------------------
    def sitemap_categories(self, session):
        """Every category URL in the sitemap (follows its page=N parts)."""
        index = fetch(f"{self.base_url}/xmlsitemap.php", session)
        if index is None:
            return []
        parts = [html.unescape(u) for u in LOC_RE.findall(index.text)
                 if "type=categories" in u]
        urls = []
        for part in parts:
            resp = fetch(part, session)
            if resp is not None:
                urls.extend(html.unescape(u) for u in LOC_RE.findall(resp.text))
        return list(dict.fromkeys(urls))

    def pick_categories(self, sitemap_urls, full_catalog):
        """Core deal pages, then discovered deal pages, minus non-cigar pages."""
        chosen = [self.url_for(c) for c in self.core_categories]
        for u in sitemap_urls:
            path = path_of(u)
            words = set(re.split(r"[^a-z0-9]+", path))
            if words & SKIP_CATEGORY_WORDS:
                continue
            is_deal = (words & DEAL_WORDS
                       and not any(p in path for p in NOT_DEAL_PHRASES))
            if is_deal or (full_catalog and self.full_catalog_category is None):
                chosen.append(u)
        if full_catalog and self.full_catalog_category:
            chosen.append(self.url_for(self.full_catalog_category))
        seen, out = set(), []
        for u in chosen:
            key = path_of(u)
            if key and key not in seen:
                seen.add(key)
                out.append(u)
        return out

    # -- crawling ----------------------------------------------------------
    def scrape_category(self, session, url, max_pages, delay, seen, stats):
        """Yields items from every page of one category not seen elsewhere."""
        page = 1
        while max_pages is None or page <= max_pages:
            resp = fetch(url if page == 1 else f"{url}?page={page}", session)
            if resp is None:
                return
            items, skipped = extract_products(resp.text, self.base_url)
            stats["skipped"] += skipped
            fresh = [i for i in items if i["url"] not in seen]
            if not fresh:
                return  # empty page, or BigCommerce repeating the last page
            for i in fresh:
                seen.add(i["url"])
                yield i
            if 'rel="next"' not in resp.text and f"page={page + 1}" not in resp.text:
                return
            page += 1
            time.sleep(delay)

    def scan(self, session, args):
        if args.categories:
            categories = [self.url_for(c) for c in args.categories]
        else:
            print("Reading category sitemap ...")
            found = self.sitemap_categories(session)
            print(f"  {len(found)} categories in sitemap")
            categories = self.pick_categories(found, args.full_catalog)
        print(f"Scanning {len(categories)} page(s)"
              + (" (full catalog)" if args.full_catalog else " (deal pages)") + "\n")

        seen, items, stats = set(), [], {"skipped": 0}
        for n, url in enumerate(categories, 1):
            before = len(items)
            items.extend(self.scrape_category(session, url, args.max_pages,
                                              args.delay, seen, stats))
            print(f"  [{n}/{len(categories)}] {path_of(url)}: "
                  f"{len(items) - before} new item(s)")
            time.sleep(args.delay)
        if stats["skipped"]:
            print(f"  skipped {stats['skipped']} tile(s): accessories, or "
                  f"multi-size listings with no reference price")
        return items, len(items)
