#!/usr/bin/env python3
"""
Crawl Hiland's Cigars and dump products to CSV with sale price and discount.

By default only items 40% off or more are written to the CSV. Change that
with --alert-pct, or pass --alert-pct 0 for the entire catalog.

Pack filtering
--------------
Pack size (single / 5-pack / box of 20) is a WooCommerce *variation*, not a
product field. So --min-pack triggers a second pass that fetches each
variation individually. That costs one extra request per variation, so a full
catalog run gets slow. Use --limit while testing.

Usage:
    pip install requests beautifulsoup4

    python hilands_crawl.py                        # 5 categories, everything
    python hilands_crawl.py --min-pack 5           # packs of 5+, no singles
    python hilands_crawl.py --max-price 75         # nothing over $75
    python hilands_crawl.py --alert-pct 25         # 25%+ instead of 40%+
    python hilands_crawl.py --alert-pct 0          # whole catalog, no filter
    python hilands_crawl.py --workers 4 --delay 0.5 # ~2 req/s, about 4x faster
    python hilands_crawl.py --min-pack 5 --limit 0 # ...whole catalog
    python hilands_crawl.py --inspect 5            # dump raw pack labels
    python hilands_crawl.py --html                 # force HTML fallback
"""

import argparse
import csv
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

BASE = "https://www.hilandscigars.com"
ROOT = f"{BASE}/shop/cigars/"

FIELDS = ["brand", "name", "pack_label", "pack_qty", "price", "regular_price",
          "sale_price", "discount", "discount_pct", "on_sale",
          "price_is_range", "currency", "sku", "in_stock", "purchasable",
          "backorder", "stock_qty", "url"]

class RateLimiter:
    """Caps requests per second across all threads."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            wait_for = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.min_interval
        if wait_for:
            time.sleep(wait_for)


RATE = RateLimiter(1.5)          # reset from --delay in main()
PRINT_LOCK = threading.Lock()    # keeps interleaved alerts readable

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
})
_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
session.mount("https://", _adapter)
session.mount("http://", _adapter)

# If pages come back empty, mirror the site's age-gate cookie here:
# session.cookies.set("age_verified", "1", domain="www.hilandscigars.com")


def get(url, **kw):
    for attempt in range(3):
        try:
            RATE.wait()
            r = session.get(url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 403:
                print("  403 (Cloudflare). Datacenter IPs are often blocked.",
                      file=sys.stderr)
                return None
            return r
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  failed: {url} ({e})", file=sys.stderr)
                return None
            time.sleep(3 * (attempt + 1))
    return None


# ---------- pack size parsing ----------

SINGLE_RE = re.compile(r"\b(single|singles|1\s*(stick|cigar)|each)\b", re.I)
PACK_RES = [
    re.compile(r"\b(?:box|pack|bundle|tin|sampler|case)\s*of\s*(\d+)", re.I),
    re.compile(r"\b(\d+)\s*[-\s]?(?:pack|pk|ct|count|cigars|sticks)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s|s)\s*(?:box|pack|tin)\b", re.I),
    re.compile(r"\bx\s*(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s)\b", re.I),
]


def parse_pack(text):
    """Return how many cigars a label describes, or None if unclear."""
    if not text:
        return None
    t = str(text)
    for rx in PACK_RES:
        m = rx.search(t)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if 1 <= n <= 200:
                return n
    if SINGLE_RE.search(t):
        return 1
    return None


def money(raw, minor_unit):
    if raw in (None, ""):
        return None
    try:
        return round(int(raw) / (10 ** minor_unit), 2)
    except (TypeError, ValueError):
        return None


def add_discount(row):
    p, rp = row.get("price"), row.get("regular_price")
    if isinstance(p, (int, float)) and isinstance(rp, (int, float)) and rp > 0:
        diff = round(rp - p, 2)
        row["discount"] = diff if diff > 0 else 0
        row["discount_pct"] = round(diff / rp * 100, 1) if diff > 0 else 0
        row["on_sale"] = diff > 0
    else:
        row.setdefault("discount", "")
        row.setdefault("discount_pct", "")
        row.setdefault("on_sale", "")
    return row


def blank_row():
    return {f: "" for f in FIELDS}


# ---------- strategy 1: Store API ----------

def api_categories():
    out, page = [], 1
    while True:
        r = get(f"{BASE}/wp-json/wc/store/v1/products/categories",
                params={"per_page": 100, "page": page})
        if r is None or r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if not data:
            break
        for c in data:
            out.append((c["id"], c["name"], c.get("count", 0)))
        page += 1
    return out


def row_from_product(p):
    pr = p.get("prices", {}) or {}
    unit = pr.get("currency_minor_unit", 2)
    rng = pr.get("price_range") or {}
    row = blank_row()
    row.update({
        "name": p.get("name", ""),
        "pack_label": "",
        "pack_qty": parse_pack(p.get("name")),
        "price": money(pr.get("price"), unit),
        "regular_price": money(pr.get("regular_price"), unit),
        "sale_price": money(pr.get("sale_price"), unit),
        "price_is_range": bool(rng),
        "currency": pr.get("currency_code", ""),
        "sku": p.get("sku", ""),
        "in_stock": p.get("is_in_stock", ""),
        "purchasable": p.get("is_purchasable", ""),
        "backorder": p.get("is_on_backorder", ""),
        "stock_qty": (p.get("stock_availability") or {}).get("text", ""),
        "url": p.get("permalink", ""),
    })
    return add_discount(row)


def api_raw_products(cat_id):
    """Yield raw product dicts for a category."""
    page = 1
    while True:
        r = get(f"{BASE}/wp-json/wc/store/v1/products",
                params={"category": cat_id, "per_page": 100, "page": page})
        if r is None or r.status_code != 200:
            return
        data = r.json()
        if not data:
            return
        for p in data:
            yield p
        page += 1


def variation_label(v):
    """Human label for a variation, from its attribute values."""
    attrs = v.get("attributes") or []
    parts = []
    for a in attrs:
        val = a.get("value") or a.get("option") or ""
        if val:
            parts.append(str(val))
    return " / ".join(parts)


def fetch_variation(parent, v):
    """One variation -> one row, or None if the fetch failed."""
    vid = v.get("id") if isinstance(v, dict) else v
    label = variation_label(v) if isinstance(v, dict) else ""
    r = get(f"{BASE}/wp-json/wc/store/v1/products/{vid}")
    if r is None or r.status_code != 200:
        return None
    try:
        vp = r.json()
    except ValueError:
        return None
    row = row_from_product(vp)
    # Variation names are usually "Parent - 5 Pack"; prefer the attribute
    # label, fall back to whatever the name gives us.
    label = label or vp.get("name", "")
    row["name"] = parent.get("name", row["name"])
    row["pack_label"] = label
    row["pack_qty"] = parse_pack(label) or parse_pack(vp.get("name")) \
        or parse_pack(parent.get("name"))
    row["price_is_range"] = False
    row["url"] = parent.get("permalink", row["url"])
    return add_discount(row)


def api_variation_rows(parent, pool):
    """All variations of a product, fetched concurrently."""
    variations = parent.get("variations") or []
    if not variations:
        return []
    futures = [pool.submit(fetch_variation, parent, v) for v in variations]
    out = []
    for f in as_completed(futures):
        row = f.result()
        if row is not None:
            out.append(row)
    return out


# ---------- strategy 2: HTML ----------

def brand_links():
    r = get(ROOT)
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pattern = re.compile("^" + re.escape(ROOT) + "[a-z0-9/-]+/$")
    seen, links = set(), []
    for a in soup.select("a[href]"):
        href = a["href"].split("?")[0].split("#")[0]
        if not href.startswith("http"):
            href = BASE + href
        if pattern.match(href) and href not in seen:
            seen.add(href)
            links.append(href)
    return links


def num(text):
    m = re.search(r"[\d,]+\.?\d*", text or "")
    if not m:
        return None
    try:
        return round(float(m.group().replace(",", "")), 2)
    except ValueError:
        return None


def html_products(url, delay):
    while url:
        r = get(url)
        if r is None:
            return
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.product, .wc-block-grid__product"):
            title = li.select_one(".woocommerce-loop-product__title, "
                                  ".wc-block-grid__product-title, h2, h3")
            if not title:
                continue
            box = li.select_one(".price, .wc-block-grid__product-price")
            old = box.select_one("del") if box else None
            new = box.select_one("ins") if box else None
            row = blank_row()
            if old is not None and new is not None:
                row["regular_price"] = num(old.get_text())
                row["price"] = num(new.get_text())
                row["sale_price"] = row["price"]
            elif box is not None:
                row["price"] = num(box.get_text())
                row["regular_price"] = row["price"]
            link = li.select_one("a[href]")
            name = title.get_text(strip=True)
            classes = " ".join(li.get("class") or [])
            sold_out = ("outofstock" in classes
                        or "out of stock" in li.get_text(" ", strip=True).lower())
            row.update({
                "name": name,
                "pack_qty": parse_pack(name),
                "in_stock": not sold_out,
                "purchasable": not sold_out,
                "url": link["href"] if link else "",
            })
            yield add_discount(row)
        nxt = soup.select_one("a.next.page-numbers, .next.page-numbers a")
        url = nxt["href"] if nxt and nxt.has_attr("href") else None
        time.sleep(delay)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5,
                    help="how many categories to crawl (0 = all). default 5")
    ap.add_argument("--min-pack", type=int, default=1,
                    help="drop anything smaller than N cigars. "
                         "use 5 to skip singles. default 1 (keep all)")
    ap.add_argument("--max-price", type=float, default=None, metavar="USD",
                    help="drop anything priced above this. no max by default")
    ap.add_argument("--include-oos", action="store_true",
                    help="keep out-of-stock items. off by default")
    ap.add_argument("--allow-backorder", action="store_true",
                    help="with stock filtering on, keep backorder items too")
    ap.add_argument("--include-unknown", action="store_true",
                    help="with --min-pack, keep rows whose pack size "
                         "could not be parsed")
    ap.add_argument("--inspect", type=int, metavar="N",
                    help="print raw variation labels for the first N "
                         "variable products, then exit")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="minimum seconds between requests, counted across "
                         "all threads. default 1.5")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent variation fetches. default 1. "
                         "4 is a reasonable ceiling for a small shop")
    ap.add_argument("--html", action="store_true", help="skip the Store API")
    ap.add_argument("--alert-pct", type=float, default=40.0,
                    help="only include items at least this much off, and "
                         "print them as they are found. default 40. "
                         "pass 0 to include the whole catalog")
    ap.add_argument("--out", default="cigars.csv")
    args = ap.parse_args()

    RATE.min_interval = args.delay
    expand = args.min_pack > 1 or bool(args.inspect)

    dropped = {"oos": 0, "pack": 0, "price": 0, "disc": 0}

    def keep(row):
        if not args.include_oos:
            if row.get("in_stock") is False or row.get("purchasable") is False:
                dropped["oos"] += 1
                return False
            if row.get("backorder") is True and not args.allow_backorder:
                dropped["oos"] += 1
                return False
        if args.min_pack > 1:
            q = row.get("pack_qty")
            if not isinstance(q, int):
                if not args.include_unknown:
                    dropped["pack"] += 1
                    return False
            elif q < args.min_pack:
                dropped["pack"] += 1
                return False
        if args.max_price is not None:
            price = row.get("price")
            if not isinstance(price, (int, float)) or price > args.max_price:
                dropped["price"] += 1
                return False
        if args.alert_pct > 0:
            pct = row.get("discount_pct")
            if not isinstance(pct, (int, float)) or pct < args.alert_pct:
                dropped["disc"] += 1
                return False
        return True

    alerted = set()

    def watch(p):
        pct = p.get("discount_pct")
        if not isinstance(pct, (int, float)) or pct < args.alert_pct:
            return
        key = (p["url"], p["name"], p.get("pack_label", ""))
        if key in alerted:
            return
        alerted.add(key)
        pack = f" [{p['pack_label']}]" if p.get("pack_label") else \
               (" (from)" if p["price_is_range"] else "")
        with PRINT_LOCK:
            print(f"    >>> {pct:>5}% OFF  {p['price']:.2f} was "
                  f"{p['regular_price']:.2f}  {p['name'][:42]}{pack}")
            print(f"        {p['url']}")
            sys.stdout.flush()

    rows = []
    cats = None if args.html else api_categories()

    if cats:
        live = [c for c in cats if c[2] > 0]
        targets = live if args.limit <= 0 else live[:args.limit]
        print(f"Store API works. {len(live)} categories with stock, "
              f"crawling {len(targets)}.")
        if expand:
            print(f"Variation pass on: one request per size, "
                  f"{args.workers} at a time, "
                  f"max one request every {args.delay:g}s overall.")
        if args.min_pack > 1:
            print(f"Filtering to packs of {args.min_pack} or more.")
        print("Skipping out-of-stock items." if not args.include_oos
              else "Including out-of-stock items.")
        if args.max_price is not None:
            print(f"Skipping anything over ${args.max_price:.2f}.")
        if args.alert_pct > 0:
            print(f"Keeping only items {args.alert_pct:g}% off or more.")
        print()

        seen_inspect = 0
        seen_products = {}   # product id -> already expanded
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        for cat_id, name, count in targets:
            print(f"  {name[:45]:<45} {count}", flush=True)
            for p in api_raw_products(cat_id):
                variations = p.get("variations") or []

                if args.inspect:
                    if variations and seen_inspect < args.inspect:
                        seen_inspect += 1
                        print(f"\n  {p.get('name', '')}")
                        for v in variations:
                            lbl = variation_label(v) if isinstance(v, dict) else ""
                            vid = v.get("id") if isinstance(v, dict) else v
                            print(f"    id={vid}  label={lbl!r}  "
                                  f"parsed_qty={parse_pack(lbl)}")
                    if seen_inspect >= args.inspect:
                        print("\nAdjust PACK_RES at the top of the script if "
                              "the parsed_qty values look wrong.")
                        return
                    continue

                pid = p.get("id")
                if pid is not None and pid in seen_products:
                    continue          # same product under a parent category
                if pid is not None:
                    seen_products[pid] = True

                if expand and variations:
                    for row in api_variation_rows(p, pool):
                        row["brand"] = name
                        if keep(row):
                            rows.append(row)
                            watch(row)
                else:
                    row = row_from_product(p)
                    row["brand"] = name
                    if keep(row):
                        rows.append(row)
                        watch(row)
        pool.shutdown(wait=True)
        print(f"\n{len(seen_products)} distinct products examined.")
    else:
        print("Store API unavailable, scraping HTML instead.")
        if args.min_pack > 1:
            print("Note: HTML mode sees only listing prices, so pack size is "
                  "guessed from product names.")
        links = brand_links()
        targets = links if args.limit <= 0 else links[:args.limit]
        print(f"{len(links)} brand pages found, crawling {len(targets)}.\n")
        for url in targets:
            brand = url.rstrip("/").split("/")[-1].replace("-", " ").title()
            print(f"  {brand}", flush=True)
            for row in html_products(url, args.delay):
                row["brand"] = brand
                if keep(row):
                    rows.append(row)
                    watch(row)

    # subcategories overlap with parent brands, so de-dupe
    seen, unique = set(), []
    for r in rows:
        key = (r["url"], r["name"], r.get("pack_label", ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)

    on_sale = [r for r in unique if r["on_sale"] is True]

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(unique)

    print(f"\nWrote {len(unique)} rows to {args.out}")
    if dropped["oos"]:
        print(f"Skipped {dropped['oos']} out-of-stock or unpurchasable.")
    if dropped["pack"]:
        print(f"Skipped {dropped['pack']} below the pack threshold.")
    if dropped["price"]:
        print(f"Skipped {dropped['price']} over the ${args.max_price:.2f} cap.")
    if dropped["disc"]:
        print(f"Skipped {dropped['disc']} under {args.alert_pct:g}% off.")
    if args.min_pack > 1:
        unknown = [r for r in unique if not isinstance(r["pack_qty"], int)]
        if unknown:
            print(f"({len(unknown)} kept with unparsed pack size)")

    big = sorted([r for r in unique if isinstance(r["discount_pct"], (int, float))
                  and r["discount_pct"] >= args.alert_pct],
                 key=lambda x: -x["discount_pct"])
    print("=" * 72)
    if big:
        print(f"{len(big)} item(s) at {args.alert_pct:g}% off or more:\n")
        for r in big:
            pack = r["pack_label"] or (f"{r['pack_qty']} ct"
                                       if r["pack_qty"] else "?")
            print(f"  {r['discount_pct']:>5}%  {r['price']:>9.2f}  was "
                  f"{r['regular_price']:>9.2f}  {r['name'][:40]}  [{pack}]")
            print(f"          {r['url']}")
    else:
        print(f"No items at {args.alert_pct:g}% off or more.")
    print("=" * 72)

    if args.limit > 0:
        print("\n(Test run. Use --limit 0 for the full catalog.)")


if __name__ == "__main__":
    main()
