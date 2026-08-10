#!/usr/bin/env python3
"""
Hiland's Cigars Deals Scraper
-----------------------------
Crawls https://www.hilandscigars.com via the WooCommerce Store API and
emails every cigar that is >= MIN_DISCOUNT off list price, in a pack of at
least MIN_PACK, at or under MAX_PRICE, and in stock.

Settings live in config.yml next to this file. Run with no arguments:

    pip install -r requirements.txt
    python hilands_crawl.py

Only CHANGED deals are emailed: items never seen before, and items whose
discount moved since the last run. Comparison is against data/latest.csv,
which this script writes itself after each successful crawl.

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set in GitHub Secrets).
"""

# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL CONFIG
# ══════════════════════════════════════════════════════════════════════════════
import os

SENDER_EMAIL    = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")   # set in GitHub Secrets
TO_EMAIL        = "peltz.chris@gmail.com"

SEND_EMAIL      = True    # False = crawl and write files, never send
FORCE           = False   # True = ignore history, treat everything as new
# ══════════════════════════════════════════════════════════════════════════════

import csv
import html
import re
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import yaml
from bs4 import BeautifulSoup

BASE = "https://www.hilandscigars.com"
ROOT = f"{BASE}/shop/cigars/"

# ── Files this script owns ────────────────────────────────────────────────────
# Paths resolve against the script's own directory, so it behaves the same
# whether launched from the repo root, from cron, or from anywhere else.
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()

OUTPUT  = os.path.join(BASE_DIR, "cigars.csv")
CHANGES = os.path.join(BASE_DIR, "cigars-changes.csv")
HISTORY = os.path.join(BASE_DIR, "data", "latest.csv")

# ── Columns ───────────────────────────────────────────────────────────────────
FIELDS = ["brand", "name", "pack_qty", "sale_price", "regular_price",
          "discount_pct", "stock_qty", "last_modified", "url"]
CHANGE_FIELDS = FIELDS + ["change"]

# Collected for filtering, never written to the CSV.
INTERNAL = ["price_is_range", "in_stock", "purchasable", "backorder",
            "pack_label", "on_sale", "change"]

# Categories that describe a promotion or format rather than a maker.
GENERIC_CATEGORIES = {
    "cigars", "samplers", "bundles", "clash packs", "hot deals", "hot deals 🔥",
    "cigar clearance", "clearance", "national brand bundles", "nic bundles",
    "sasc bundles", "cra samplers", "shop", "accessories",
}

DEFAULTS = {
    "categories": 0,
    "min_discount": 40.0,
    "min_pack": 5,
    "max_price": 99.99,
    "include_out_of_stock": False,
    "allow_backorder": False,
    "include_unknown_pack": False,
    "workers": 1,
    "delay": 2.0,
    "inspect": 0,
}


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(argv):
    """config.yml next to this script, unless a path is passed."""
    path = argv[1] if len(argv) > 1 else os.path.join(BASE_DIR, "config.yml")

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
    if loaded.get("max_price") in (None, ""):
        cfg["max_price"] = None      # blank means no cap

    for key in ("categories", "min_pack", "workers", "inspect"):
        cfg[key] = int(cfg[key])
    for key in ("min_discount", "delay"):
        cfg[key] = float(cfg[key])
    if cfg["max_price"] is not None:
        cfg["max_price"] = float(cfg["max_price"])

    print(f"Config: {os.path.basename(path)}")
    return cfg


def describe(cfg):
    return "\n".join([
        f"  categories:    {'all' if cfg['categories'] <= 0 else cfg['categories']}",
        f"  min discount:  {cfg['min_discount']:g}%"
        + ("  (no filter)" if cfg["min_discount"] <= 0 else ""),
        f"  min pack:      {cfg['min_pack']}"
        + ("  (singles kept)" if cfg["min_pack"] <= 1 else "  (singles skipped)"),
        "  max price:     "
        + ("none" if cfg["max_price"] is None else f"${cfg['max_price']:.2f}"),
        "  stock:         "
        + ("including out of stock" if cfg["include_out_of_stock"]
           else "in stock only"),
        f"  rate:          {cfg['workers']} worker(s), "
        f"{cfg['delay']:g}s between requests",
        f"  email:         "
        + (f"on -> {TO_EMAIL}" if SEND_EMAIL else "off"),
    ])


# ── Change tracking ───────────────────────────────────────────────────────────

def load_history(path):
    """Previous run keyed by url -> (discount_pct, last_modified)."""
    if not path or not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                url = (row.get("url") or "").strip()
                if not url:
                    continue
                try:
                    pct = round(float(row.get("discount_pct") or 0), 1)
                except ValueError:
                    pct = None
                out[url] = (pct, (row.get("last_modified") or "").strip())
    except OSError as e:
        print(f"Could not read history at {path}: {e}")
        return {}
    return out


def mark_changes(rows, history, today):
    """Stamp last_modified, and return the subset that moved today.

    A record is 'modified' when it is first seen, or when its discount
    percentage changes. Everything else keeps the date it last moved.
    """
    changed = []
    for r in rows:
        try:
            pct = round(float(r["discount_pct"]), 1)
        except (TypeError, ValueError):
            pct = None
        prior = history.get((r.get("url") or "").strip())

        if prior is None:
            r["last_modified"] = today
            r["change"] = "new"
        elif prior[0] is None or pct is None or abs(pct - prior[0]) >= 0.05:
            r["last_modified"] = today
            r["change"] = (f"{prior[0]:g}% -> {pct:g}%"
                           if prior[0] is not None and pct is not None
                           else "discount changed")
        else:
            r["last_modified"] = prior[1] or today
            r["change"] = ""

        if r["change"]:
            changed.append(r)
    return changed


# ── Plumbing ──────────────────────────────────────────────────────────────────

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


RATE = RateLimiter(2.0)
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
            if r.status_code == 503:
                print("  HTTP 503, the site is rate limiting or briefly down.")
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 403:
                print(f"  HTTP 403 from {r.headers.get('server', '?')}. "
                      f"This IP is blocked; the same code often works from a "
                      f"home connection.")
                return None
            return r
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  failed: {url} ({type(e).__name__}: {e})")
                return None
            time.sleep(3 * (attempt + 1))
    return None


# ── Parsers ───────────────────────────────────────────────────────────────────

SINGLE_RE = re.compile(r"\b(single|singles|1\s*(stick|cigar)|each)\b", re.I)
PACK_RES = [
    re.compile(r"\b(?:box|pack|bundle|tin|sampler|case)\s*of\s*(\d+)", re.I),
    re.compile(r"\b(\d+)\s*[-\s]?(?:pack|pk|ct|count|cigars|sticks)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s|s)\s*(?:box|pack|tin)\b", re.I),
    re.compile(r"\bx\s*(\d+)\b", re.I),
    re.compile(r"\b(\d+)\s*(?:'s)\b", re.I),
]


def clean(text):
    """Decode HTML entities and tidy whitespace. 5&#215;54 -> 5x54."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(text))).strip()


def parse_pack(text):
    """How many cigars a name or label describes, or None if unclear."""
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
    """Store API sends integer minor units. 1299 with unit 2 -> 12.99"""
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
    p, rp = row.get("sale_price"), row.get("regular_price")
    if isinstance(p, (int, float)) and isinstance(rp, (int, float)) and rp > 0:
        diff = round(rp - p, 2)
        row["discount_pct"] = round(diff / rp * 100, 1) if diff > 0 else 0
        row["on_sale"] = diff > 0
    else:
        row.setdefault("discount_pct", "")
        row.setdefault("on_sale", "")
    return row


def blank_row():
    return {f: "" for f in FIELDS + INTERNAL}


def pick_brand(product, fallback):
    """Prefer the product's own maker category over a promo category."""
    for c in product.get("categories") or []:
        name = clean(c.get("name"))
        if name and name.lower() not in GENERIC_CATEGORIES:
            return name
    return fallback


# ── Store API ─────────────────────────────────────────────────────────────────

def api_categories():
    """All product categories, or None with a reason printed."""
    out, page = [], 1
    url = f"{BASE}/wp-json/wc/store/v1/products/categories"
    while True:
        r = get(url, params={"per_page": 100, "page": page})
        if r is None:
            print("  no response (network error or blocked).")
            return None
        if r.status_code != 200:
            print(f"  Store API returned HTTP {r.status_code}")
            print("  body starts:", " ".join((r.text or "")[:200].split()))
            return None
        try:
            data = r.json()
        except ValueError:
            print("  not JSON, content-type:",
                  r.headers.get("content-type", "?"))
            print("  body starts:", " ".join((r.text or "")[:200].split()))
            print("  A challenge page here means Cloudflare is blocking "
                  "this IP.")
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
    name = clean(p.get("name"))
    sale = money(pr.get("sale_price"), unit)
    if sale is None:
        sale = money(pr.get("price"), unit)   # not on sale, or field left null
    row = blank_row()
    row.update({
        "name": name,
        "pack_qty": parse_pack(name),
        "sale_price": sale,
        "regular_price": money(pr.get("regular_price"), unit),
        "price_is_range": bool(pr.get("price_range") or {}),
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
        try:
            data = r.json()
        except ValueError:
            return
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


# ── HTML fallback, used only if the Store API is closed ───────────────────────

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
                row["sale_price"] = num(new.get_text())
            elif box is not None:
                row["sale_price"] = num(box.get_text())
                row["regular_price"] = row["sale_price"]
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


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_body(changed, total, cfg):
    """Plain text body, grouped by brand, new items before re-priced ones."""
    new_items = [c for c in changed if c["change"] == "new"]
    moved = [c for c in changed if c["change"] != "new"]

    cap = ("" if cfg["max_price"] is None
           else f", at or under ${cfg['max_price']:.0f}")
    lines = [
        f"{len(changed)} change(s) at Hiland's: {len(new_items)} new, "
        f"{len(moved)} re-priced.",
        f"Filters: {cfg['min_discount']:g}% off or more, packs of "
        f"{cfg['min_pack']}+{cap}, in stock.",
        f"{total} item(s) currently qualify in total.",
        "=" * 60,
    ]

    def block(title, items):
        if not items:
            return
        lines.append(f"\n{title.upper()} ({len(items)})")
        by_brand = {}
        for c in items:
            by_brand.setdefault(c.get("brand") or "Other", []).append(c)
        order = sorted((b for b in by_brand if b != "Other"), key=str.lower)
        if "Other" in by_brand:
            order.append("Other")
        for b in order:
            rows = sorted(by_brand[b], key=lambda c: c["name"].lower())
            lines.append(f"\n---- {b} ({len(rows)}) ----")
            for c in rows:
                note = "" if c["change"] == "new" else f"   [{c['change']}]"
                lines.extend([
                    f"\n{c['name']}",
                    f"  ${c['sale_price']:.2f}  was ${c['regular_price']:.2f}  "
                    f"({c['discount_pct']:g}% off)  "
                    f"{c['pack_qty'] or '?'} count{note}",
                    f"  {c['url']}",
                ])

    block("New deals", new_items)
    block("Discount changed", moved)
    lines.extend(["\n" + "=" * 60, "\nFull results attached as CSV."])
    return "\n".join(lines)


def send_email(changed, total, cfg):
    """Returns True if the mail went out."""
    if not SEND_EMAIL:
        print("Email is switched off (SEND_EMAIL = False).")
        return False
    if not changed:
        print("Nothing changed since the last run, so no email sent.")
        return False
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. The changes are still in the CSV.")
        return False

    print(f"Sending email to {TO_EMAIL} ...")
    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = TO_EMAIL
    new_count = sum(1 for c in changed if c["change"] == "new")
    msg["Subject"] = (f"Hiland's: {len(changed)} change(s), "
                      f"{new_count} new at {cfg['min_discount']:g}%+ off")
    msg.attach(MIMEText(build_email_body(changed, total, cfg), "plain"))

    for path, label in ((CHANGES, "hilands_changes.csv"),
                        (OUTPUT, "hilands_all_deals.csv")):
        try:
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{label}"')
            msg.attach(part)
        except OSError as e:
            print(f"  could not attach {path}: {e}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, TO_EMAIL, msg.as_string())
        print(f"Email sent to {TO_EMAIL}")
        return True
    except Exception as e:
        print(f"Email failed: {type(e).__name__}: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    started = time.time()
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
            price = row.get("sale_price")
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
            print(f"    >>> {row['discount_pct']:>5}% OFF  "
                  f"{row['sale_price']:.2f} was {row['regular_price']:.2f}  "
                  f"{row['name'][:42]}{pack}")
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
                        print(f"\n  {clean(p.get('name'))}")
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
                        continue      # same product under a parent category
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

    # De-dupe: the same product appears under parent and child categories.
    seen, unique = set(), []
    for r in rows:
        key = (r["url"], r["name"], r.get("pack_label", ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)

    today = time.strftime("%Y-%m-%d")
    history = {} if FORCE else load_history(HISTORY)

    # A crawl returning nothing, or a fraction of last time, means something
    # broke rather than the shop emptying out. Writing those files would
    # destroy the baseline and make everything look new next run, so stop
    # here and leave the previous data untouched.
    if not unique:
        print("\nNo products captured. Previous files left untouched.")
        print("Check the messages above: a block, a markup change, or "
              "filters that are too strict.")
        sys.exit(1)

    if history and len(unique) < len(history) * 0.5:
        print(f"\nOnly {len(unique)} rows, down from {len(history)} last run. "
              f"That looks truncated rather than real.")
        print("Previous files left untouched. Re-run to confirm, or delete "
              "data/latest.csv if the drop is genuine.")
        sys.exit(1)

    changed = mark_changes(unique, history, today)

    def write(path, data, fields):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    write(OUTPUT, unique, FIELDS)
    write(CHANGES, changed, CHANGE_FIELDS)
    write(HISTORY, unique, FIELDS)      # baseline for the next run

    print(f"\nWrote {len(unique)} rows to {os.path.basename(OUTPUT)}")
    if history:
        new_count = sum(1 for r in changed if r["change"] == "new")
        print(f"Wrote {len(changed)} changed rows to "
              f"{os.path.basename(CHANGES)} "
              f"({new_count} new, {len(changed) - new_count} re-priced)")
    elif FORCE:
        print(f"Force mode, so all {len(changed)} rows count as new.")
    else:
        print(f"First run, no baseline yet, so all {len(changed)} rows count "
              f"as new. Next run will show real changes.")
    print("Baseline updated: data/latest.csv")

    for key, msg in (("oos", "out of stock"),
                     ("pack", f"under {cfg['min_pack']} count"),
                     ("price", "over the price cap"),
                     ("disc", f"under {cfg['min_discount']:g}% off")):
        if dropped[key]:
            print(f"  skipped {dropped[key]} {msg}")

    print("=" * 72)
    if changed:
        print(f"{len(changed)} change(s) this run:\n")
        for r in sorted(changed, key=lambda x: -x["discount_pct"]):
            note = "NEW" if r["change"] == "new" else r["change"]
            print(f"  {r['discount_pct']:>5}%  {r['sale_price']:>8.2f}  was "
                  f"{r['regular_price']:>8.2f}  {r['name'][:38]}  ({note})")
            print(f"          {r['url']}")
    else:
        print("No changes since the last run.")
    print("=" * 72)

    send_email(changed, len(unique), cfg)
    print(f"\nFinished in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
