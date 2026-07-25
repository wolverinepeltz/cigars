#!/usr/bin/env python3
"""
Crawl Hiland's Cigars and record discounted stock.

Everything is configured in config.yml sitting next to this file.
Run it with no arguments:

    pip install -r requirements.txt
    python hilands_crawl.py

Optionally point it at a different config:

    python hilands_crawl.py other-config.yml

Pack sizes (single, 5-pack, box of 25) are WooCommerce variations, so when
min_pack is above 1 the crawler fetches each size separately. That is the
slow part. Products repeated across parent and child categories are only
fetched once.
"""

import csv
import html
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://www.hilandscigars.com"
ROOT = f"{BASE}/shop/cigars/"

FIELDS = ["brand", "name", "pack_label", "pack_qty", "price", "regular_price",
          "sale_price", "discount", "discount_pct", "on_sale",
          "price_is_range", "currency", "sku", "in_stock", "purchasable",
          "backorder", "stock_qty", "url"]

# Categories that describe a promotion or format, not a maker.
GENERIC_CATEGORIES = {
    "cigars", "samplers", "bundles", "clash packs", "hot deals", "hot deals 🔥",
    "cigar clearance", "clearance", "national brand bundles", "nic bundles",
    "sasc bundles", "cra samplers", "shop", "accessories",
}


def pick_brand(product, fallback):
    """Prefer the product's own maker category over a promo category."""
    for c in product.get("categories") or []:
        name = clean(c.get("name"))
        if name and name.lower() not in GENERIC_CATEGORIES:
            return name
    return fallback


DEFAULTS = {
    "categories": 25,
    "min_discount": 40.0,
    "min_pack": 5,
    "max_price": None,
    "include_out_of_stock": False,
    "allow_backorder": False,
    "include_unknown_pack": False,
    "workers": 4,
    "delay": 0.5,
    "output": "cigars.csv",
    "inspect": 0,
}


# ---------- config ----------

def load_config(argv):
    """config.yml next to this script, unless a path is given."""
    if len(argv) > 1:
        path = argv[1]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "config.yml")

    cfg = dict(DEFAULTS)
    if not os.path.exists(path):
        print(f"No config at {path}, using built-in defaults.")
        return cfg

    with open(path) as f:
        loaded = yaml.safe_load(f) or {}

    unknown = set(loaded) - set(DEFAULTS)
    if unknown:
        sys.exit(f"Unknown setting(s) in {path}: {', '.join(sorted(unknown))}\n"
                 f"Valid settings: {', '.join(sorted(DEFAULTS))}")

    cfg.update({k: v for k, v in loaded.items() if v is not None})
    # blank max_price means no cap, so it stays None
    if loaded.get("max_price") in (None, ""):
        cfg["max_price"] = None

    for key in ("categories", "min_pack", "workers", "inspect"):
        cfg[key] = int(cfg[key])
    for key in ("min_discount", "delay"):
        cfg[key] = float(cfg[key])
    if cfg["max_price"] is not None:
        cfg["max_price"] = float(cfg["max_price"])
    print(f"Config: {os.path.basename(path)}")
    return cfg


def describe(cfg):
    cats = "all" if cfg["categories"] <= 0 else cfg["categories"]
    lines = [
        f"  categories:    {cats}",
        f"  min discount:  {cfg['min_discount']:g}%"
        + ("  (no filter)" if cfg["min_discount"] <= 0 else ""),
        f"  min pack:      {cfg['min_pack']}"
        + ("  (singles kept)" if cfg["min_pack"] <= 1 else "  (singles skipped)"),
        f"  max price:     "
        + ("none" if cfg["max_price"] is None else f"${cfg['max_price']:.2f}"),
        f"  stock:         "
        + ("including out of stock" if cfg["include_out_of_stock"]
           else "in stock only"),
        f"  rate:          {cfg['workers']} workers, "
        f"{cfg['delay']:g}s between requests",
        f"  output:        {cfg['output']}",
    ]
    return "\n".join(lines)


# ---------- plumbing ----------

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


RATE = RateLimiter(0.5)
PRINT_LOCK = threading.Lock()

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


# ---------- parsing helpers ----------

SINGLE_RE = re.compile(r"\b(single|singles|1\s*(stick|cigar)|each)\b", re.I)
PACK_RES = [
    re.compile(r"\b(?:box|pack|bundle|tin|sampler|case)\s*of\s*(\d+)", re.I),
    re.compile(r"\b(\d+)\s*[-\s]?(?:pack|pk|ct|count|cigars|sticks)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s|s)\s*(?:box|pack|tin)\b", re.I),
    re.compile(r"\bx\s*(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s)\b", re.I),
]


def clean(text):
    """Decode HTML entities and tidy whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


def parse_pack(text):
    """How many cigars a label describes, or None if unclear."""
    if not text:
        return None
    t = clean(text)
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
    """Store API sends integer cents. 1299 with unit 2 -> 12.99"""
    if raw in (None, ""):
        return None
    try:
        return round(int(raw) / (10 ** minor_unit), 2)
    except (TypeError, ValueError):
        return None


def num(text):
    m = re.search(r"[\d,]+\.?\d*", text or "")
    if not m:
        return None
    try:
        return round(float(m.group().replace(",", "")), 2)
    except ValueError:
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


# ---------- Store API ----------

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
    row = blank_row()
    name = clean(p.get("name"))
    row.update({
        "name": name,
        "pack_qty": parse_pack(name),
        "price": money(pr.get("price"), unit),
        "regular_price": money(pr.get("regular_price"), unit),
        "sale_price": money(pr.get("sale_price"), unit),
        "price_is_range": bool(pr.get("price_range") or {}),
        "currency": pr.get("currency_code", ""),
        "sku": p.get("sku", ""),
        "in_stock": p.get("is_in_stock", ""),
        "purchasable": p.get("is_purchasable", ""),
        "backorder": p.get("is_on_backorder", ""),
        "stock_qty": (p.get("stock_availability") or {}).get("text", ""),
        "url": p.get("permalink", ""),
    })
    return add_discount(row)


def api_products(cat_id):
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
    parts = [clean(a.get("value") or a.get("option") or "")
             for a in (v.get("attributes") or [])]
    return " / ".join(x for x in parts if x)


def fetch_variation(parent, v):
    """One size of one product -> one row."""
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
    label = label or clean(vp.get("name"))
    row["name"] = clean(parent.get("name")) or row["name"]
    row["pack_label"] = label
    row["pack_qty"] = (parse_pack(label) or parse_pack(vp.get("name"))
                       or parse_pack(parent.get("name")))
    row["price_is_range"] = False
    row["url"] = parent.get("permalink", row["url"])
    return add_discount(row)


def variation_rows(parent, pool):
    variations = parent.get("variations") or []
    if not variations:
        return []
    futures = [pool.submit(fetch_variation, parent, v) for v in variations]
    return [r for r in (f.result() for f in as_completed(futures)) if r]


# ---------- HTML fallback, used only if the API is closed ----------

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


def html_products(url):
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
            name = clean(title.get_text(strip=True))
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


# ---------- main ----------

def main():
    cfg = load_config(sys.argv)
    RATE.min_interval = cfg["delay"]
    print(describe(cfg) + "\n")

    dropped = {"oos": 0, "pack": 0, "price": 0, "disc": 0}
    alerted = set()

    def keep(row):
        if not cfg["include_out_of_stock"]:
            if row.get("in_stock") is False or row.get("purchasable") is False:
                dropped["oos"] += 1
                return False
            if row.get("backorder") is True and not cfg["allow_backorder"]:
                dropped["oos"] += 1
                return False
        if cfg["min_pack"] > 1:
            q = row.get("pack_qty")
            if not isinstance(q, int):
                if not cfg["include_unknown_pack"]:
                    dropped["pack"] += 1
                    return False
            elif q < cfg["min_pack"]:
                dropped["pack"] += 1
                return False
        if cfg["max_price"] is not None:
            price = row.get("price")
            if not isinstance(price, (int, float)) or price > cfg["max_price"]:
                dropped["price"] += 1
                return False
        if cfg["min_discount"] > 0:
            pct = row.get("discount_pct")
            if not isinstance(pct, (int, float)) or pct < cfg["min_discount"]:
                dropped["disc"] += 1
                return False
        return True

    def announce(row):
        key = (row["url"], row["name"], row.get("pack_label", ""))
        if key in alerted:
            return
        alerted.add(key)
        pack = (f" [{row['pack_label']}]" if row.get("pack_label")
                else (" (from)" if row["price_is_range"] else ""))
        with PRINT_LOCK:
            print(f"    >>> {row['discount_pct']:>5}% OFF  {row['price']:.2f} "
                  f"was {row['regular_price']:.2f}  {row['name'][:42]}{pack}")
            print(f"        {row['url']}")
            sys.stdout.flush()

    rows = []
    cats = api_categories()

    if cats:
        live = [c for c in cats if c[2] > 0]
        targets = live if cfg["categories"] <= 0 else live[:cfg["categories"]]
        print(f"Store API works. {len(live)} categories with stock, "
              f"crawling {len(targets)}.\n")

        expand = cfg["min_pack"] > 1 or cfg["inspect"] > 0
        seen_products = set()
        inspected = 0
        pool = ThreadPoolExecutor(max_workers=max(1, cfg["workers"]))

        for cat_id, name, count in targets:
            print(f"  {name[:45]:<45} {count}", flush=True)
            for p in api_products(cat_id):
                variations = p.get("variations") or []

                if cfg["inspect"] > 0:
                    if variations and inspected < cfg["inspect"]:
                        inspected += 1
                        print(f"\n  {p.get('name', '')}")
                        for v in variations:
                            lbl = variation_label(v) if isinstance(v, dict) else ""
                            print(f"    label={lbl!r}  parsed={parse_pack(lbl)}")
                    if inspected >= cfg["inspect"]:
                        print("\nIf parsed values look wrong, adjust PACK_RES "
                              "near the top of this script.")
                        pool.shutdown(wait=False)
                        return
                    continue

                pid = p.get("id")
                if pid is not None:
                    if pid in seen_products:
                        continue    # same product under a parent category
                    seen_products.add(pid)

                found = (variation_rows(p, pool) if expand and variations
                         else [row_from_product(p)])
                brand = pick_brand(p, name)
                for row in found:
                    row["brand"] = brand
                    if keep(row):
                        rows.append(row)
                        announce(row)

        pool.shutdown(wait=True)
        print(f"\n{len(seen_products)} distinct products examined.")
    else:
        print("Store API unavailable, reading the shop pages instead.\n")
        links = brand_links()
        targets = links if cfg["categories"] <= 0 else links[:cfg["categories"]]
        print(f"{len(links)} brand pages found, crawling {len(targets)}.\n")
        for url in targets:
            brand = url.rstrip("/").split("/")[-1].replace("-", " ").title()
            print(f"  {brand}", flush=True)
            for row in html_products(url):
                row["brand"] = brand
                if keep(row):
                    rows.append(row)
                    announce(row)

    seen, unique = set(), []
    for r in rows:
        key = (r["url"], r["name"], r.get("pack_label", ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)

    with open(cfg["output"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(unique)

    print(f"\nWrote {len(unique)} rows to {cfg['output']}")
    if dropped["oos"]:
        print(f"  skipped {dropped['oos']} out of stock")
    if dropped["pack"]:
        print(f"  skipped {dropped['pack']} under {cfg['min_pack']} count")
    if dropped["price"]:
        print(f"  skipped {dropped['price']} over ${cfg['max_price']:.2f}")
    if dropped["disc"]:
        print(f"  skipped {dropped['disc']} under {cfg['min_discount']:g}% off")

    deals = sorted([r for r in unique
                    if isinstance(r["discount_pct"], (int, float))],
                   key=lambda x: -x["discount_pct"])
    print("=" * 72)
    if deals:
        print(f"{len(deals)} item(s) at {cfg['min_discount']:g}% off or more:\n")
        for r in deals:
            pack = r["pack_label"] or (f"{r['pack_qty']} ct"
                                       if r["pack_qty"] else "?")
            print(f"  {r['discount_pct']:>5}%  {r['price']:>9.2f}  was "
                  f"{r['regular_price']:>9.2f}  {r['name'][:40]}  [{pack}]")
            print(f"          {r['url']}")
    else:
        print(f"Nothing at {cfg['min_discount']:g}% off or more.")
    print("=" * 72)

    if cfg["categories"] > 0:
        print(f"\nOnly {cfg['categories']} categories checked. "
              f"Set categories: 0 in config.yml for the whole catalog.")


if __name__ == "__main__":
    main()
