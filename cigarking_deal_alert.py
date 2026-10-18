#!/usr/bin/env python3
"""
cigarking_deal_alert.py - watch cigarking.com for deals and email new ones.

Cigar King runs on BigCommerce (Stencil theme). Unlike Shopify (see
gtcigars_deal_alert.py) there is no public JSON product feed, so this reads
the product tiles on category listing pages, 100 per page, ?page=N.

Each tile carries up to three prices, in this order (labels are hidden by
the theme, but the markup has data attributes for each):
    MSRP     [data-product-rrp-price-without-tax]
    Was      [data-product-non-sale-price-without-tax]
    Now      [data-product-price-without-tax]
Discount is worked out from Was -> Now (what the site itself advertises),
falling back to MSRP -> Now when there's no Was price. If those attributes
are ever missing, it falls back to reading the dollar amounts in the tile:
the last one is Now, the highest earlier one is the reference.

Each run:
  1. Reads the category sitemap and picks the deal categories (Weekly
     Deals, Slam Packs, clearance / "% off" / special promo pages; Cigar
     King creates new ones per promotion, so they're discovered fresh each
     run rather than hard-coded).
     With --full-catalog it scans every category instead.
  2. Keeps in-stock items that pass EITHER rule:
       a) discount >= --min-discount (40%) AND price <= --max-price ($125)
       b) discount >= --alt-min-discount (25%) AND price between
          --alt-min-price ($100) and --alt-max-price ($125)
  3. Diffs against cigarking_sent_history.json (url -> last-notified
     price). "New" means never seen before, OR now cheaper than when it
     was last emailed.
  4. Emails only the NEW matches (if any) and updates the history file.

Nothing is emailed if there's nothing new.

Setup
-----
    pip install requests beautifulsoup4 --break-system-packages

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set as a GitHub Actions secret when run on a
schedule). Sender/recipient are set as constants below.

    export GMAIL_PASSWORD='app-specific-password'

Usage
-----
    python cigarking_deal_alert.py
    python cigarking_deal_alert.py --dry-run
    python cigarking_deal_alert.py --full-catalog
    python cigarking_deal_alert.py --categories weekly-deals slam-packs
    python cigarking_deal_alert.py --max-pages 1             # quick test

Caveat
------
Built from the live site's prices and page structure as read through a
fetch tool, but not run end-to-end against the site (my sandbox can't
reach it). Run once with --dry-run to confirm.
"""

import argparse
import html
import json
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.cigarking.com"
SITEMAP_INDEX = f"{BASE_URL}/xmlsitemap.php"

# Always scanned, even if the sitemap can't be read. Add any page here that
# Cigar King leaves out of its sitemap, such as the email-only "hidden
# offers" sale. If one is retired the run just logs a 404 and moves on.
CORE_DEAL_CATEGORIES = [
    "weekly-deals",
    "slam-packs",
    "weekend-preview-sale-hidden-offers",
]

# A category slug containing any of these words counts as a deal page.
# ("red" is left out on purpose: it matches lines like Cohiba Red Dot.
#  Red-tag sale pages are still caught by "sale" / "off".)
DEAL_WORDS = {
    "deal", "deals", "sale", "clearance", "special", "specials", "off",
    "slam", "closeout", "blowout", "discount", "steal", "steals",
    "overstock",
}
# Product-line names that contain a deal word but aren't promos.
NOT_DEAL_PHRASES = ("special-selection", "special-edition", "special-reserve")
# Never worth scanning for cigar deals.
SKIP_WORDS = {
    "humidor", "humidors", "accessories", "accessory", "lighter",
    "lighters", "cutter", "cutters", "ashtray", "ashtrays", "gift",
    "apparel", "case", "cases", "lifestyle",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

HISTORY_FILE = Path(__file__).parent / "cigarking_sent_history.json"

#  EMAIL CONFIG (matches the pattern used elsewhere in this repo)
SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
RANGE_RE = re.compile(r"\$\s*[\d,.]+\s*[-–]\s*\$\s*[\d,.]+")
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
OUT_OF_STOCK_RE = re.compile(r"\b(out of stock|sold out)\b", re.I)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str, session: requests.Session) -> str | None:
    """Page text, or None on failure. Backs off on 429/503."""
    for attempt in range(3):
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
        except requests.RequestException as e:
            print(f"  request failed ({type(e).__name__}: {e})")
            time.sleep(3 * (attempt + 1))
            continue
        if resp.status_code in (429, 503):
            wait = 10 * (attempt + 1)
            print(f"  HTTP {resp.status_code}, waiting {wait}s ...")
            time.sleep(wait)
            continue
        if resp.status_code == 403:
            print("  HTTP 403 - this IP is blocked. The same code often "
                  "works from a home connection.")
            return None
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}")
            return None
        return resp.text
    return None


def slug_of(url: str) -> str:
    return urlparse(url).path.strip("/").lower()


def category_urls(session: requests.Session) -> list[str]:
    """Every category URL in the sitemap (follows its page=N parts)."""
    index = fetch(SITEMAP_INDEX, session)
    if not index:
        return []
    parts = [html.unescape(u) for u in LOC_RE.findall(index)
             if "type=categories" in u]
    urls = []
    for part in parts:
        text = fetch(part, session)
        if text:
            urls.extend(html.unescape(u) for u in LOC_RE.findall(text))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def pick_categories(all_urls: list[str], full_catalog: bool) -> list[str]:
    """Core deal pages first, then discovered ones, minus non-cigar pages."""
    chosen = [f"{BASE_URL}/{c}/" for c in CORE_DEAL_CATEGORIES]
    for u in all_urls:
        words = set(re.split(r"[^a-z0-9]+", slug_of(u)))
        if words & SKIP_WORDS:
            continue
        is_deal = (words & DEAL_WORDS
                   and not any(p in slug_of(u) for p in NOT_DEAL_PHRASES))
        if full_catalog or is_deal:
            chosen.append(u)
    seen, out = set(), []
    for u in chosen:
        key = slug_of(u)
        if key and key not in seen:
            seen.add(key)
            out.append(u)
    return out


# --------------------------------------------------------------------------
# Parsing product tiles
# --------------------------------------------------------------------------

def money(text: str | None) -> float | None:
    """'$118.75' -> 118.75; empty or a range like '$10 - $20' -> None."""
    if not text or RANGE_RE.search(text):
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def find_cards(soup: BeautifulSoup) -> list:
    """Product tiles. Stencil marks them with data-entity-id."""
    cards = soup.select("article.card[data-entity-id], [data-entity-id].card")
    if not cards:
        cards = soup.select("article.card, li.product, .productGrid .product")
    return cards


def card_prices(card) -> tuple[float | None, float | None, bool]:
    """Returns (now, reference, is_range)."""
    def pick(*selectors):
        for sel in selectors:
            el = card.select_one(sel)
            if el is not None:
                return el.get_text(" ", strip=True)
        return None

    now_txt = pick("[data-product-price-without-tax]", ".price--withoutTax",
                   ".price--main")
    was_txt = pick("[data-product-non-sale-price-without-tax]",
                   ".price--non-sale")
    rrp_txt = pick("[data-product-rrp-price-without-tax]", ".price--rrp")

    if now_txt is not None:
        if RANGE_RE.search(now_txt):
            return None, None, True
        now = money(now_txt)
        was, rrp = money(was_txt), money(rrp_txt)
        if was and now and was > now:
            return now, was, False
        if rrp and now and rrp > now:
            return now, rrp, False
        return now, None, False

    # Fallback: no recognisable price markup, read the dollar amounts.
    text = card.get_text(" ", strip=True)
    if RANGE_RE.search(text):
        return None, None, True
    amounts = [float(a.replace(",", "")) for a in PRICE_RE.findall(text)]
    if not amounts:
        return None, None, False
    now = amounts[-1]
    earlier = [a for a in amounts[:-1] if a > now]
    return now, (max(earlier) if earlier else None), False


def extract_products(page_html: str) -> tuple[list[dict], int]:
    """Returns (products, number of price-range tiles skipped)."""
    soup = BeautifulSoup(page_html, "html.parser")
    products, ranges = [], 0

    for card in find_cards(soup):
        link = card.select_one(".card-title a[href], h3 a[href], h4 a[href]")
        if link is None:
            link = next((a for a in card.select("a[href]")
                         if a.get_text(strip=True)
                         and "cart.php" not in a["href"]), None)
        if link is None:
            continue
        url = urljoin(BASE_URL, link["href"]).split("?")[0].split("#")[0]
        name = link.get_text(" ", strip=True)
        if not name:
            continue

        now, ref, is_range = card_prices(card)
        if is_range:
            ranges += 1
            continue
        if now is None:
            continue

        discount_pct = round((ref - now) / ref * 100, 1) if ref else 0

        products.append({
            "name": name,
            "url": url,
            "price": now,
            "was": ref,
            "discount_pct": discount_pct,
            "in_stock": not OUT_OF_STOCK_RE.search(card.get_text(" ", strip=True)),
        })
    return products, ranges


# --------------------------------------------------------------------------
# Crawling
# --------------------------------------------------------------------------

def qualifies(row: dict, rules: dict) -> bool:
    """True if an in-stock item passes either the main or the alternate rule."""
    if not row["in_stock"]:
        return False
    pct, price = row["discount_pct"], row["price"]
    main = pct >= rules["min_discount"] and price <= rules["max_price"]
    alt = (pct >= rules["alt_min_discount"]
           and rules["alt_min_price"] <= price <= rules["alt_max_price"])
    return main or alt


def scrape_category(session, url, max_pages, delay, seen_urls, stats):
    """Yields products from every page of one category not seen elsewhere."""
    page = 1
    while max_pages is None or page <= max_pages:
        page_url = url if page == 1 else f"{url}?page={page}"
        text = fetch(page_url, session)
        if text is None:
            return
        products, ranges = extract_products(text)
        stats["ranges"] += ranges

        fresh = [p for p in products if p["url"] not in seen_urls]
        if not fresh:
            return  # empty page, or BigCommerce repeating the last page
        for p in fresh:
            seen_urls.add(p["url"])
            yield p

        if 'rel="next"' not in text and f"page={page + 1}" not in text:
            return
        page += 1
        time.sleep(delay)


def scrape_deals(session, categories, rules, max_pages=None, delay=1.5):
    """Returns (matches, items examined)."""
    seen_urls, matches = set(), []
    stats = {"ranges": 0}
    examined = 0

    for i, url in enumerate(categories, 1):
        before = examined
        for p in scrape_category(session, url, max_pages, delay, seen_urls, stats):
            examined += 1
            if qualifies(p, rules):
                matches.append(p)
        print(f"  [{i}/{len(categories)}] {slug_of(url)}: "
              f"{examined - before} new item(s)")
        time.sleep(delay)

    if stats["ranges"]:
        print(f"  skipped {stats['ranges']} tile(s) priced as a range "
              f"(multi-size listings)")
    return matches, examined


# --------------------------------------------------------------------------
# History (dedup across runs, re-notify on a deeper discount)
# --------------------------------------------------------------------------

def load_history(path: Path) -> dict:
    """Returns {url: last_notified_price}."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # back-compat with a flat-list format
        return {url: None for url in data}
    return data


def save_history(path: Path, history: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def describe(item: dict) -> str:
    was = f" (was ${item['was']:.2f})" if item["was"] else ""
    return (f"{item['name']}\n"
            f"  ${item['price']:.2f}{was}, {item['discount_pct']:g}% off\n"
            f"  {item['url']}")


def send_email(new_items: list[dict]) -> bool:
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. History not updated, so these will retry next run.")
        return False

    ordered = sorted(new_items, key=lambda x: (-x["discount_pct"], x["price"]))
    body = (f"Found {len(new_items)} new cigar deal(s) on Cigar King:\n\n"
            + "\n\n".join(describe(i) for i in ordered))

    msg = MIMEText(body)
    msg["Subject"] = f"{len(new_items)} new cigar deal(s) on Cigar King"
    msg["From"] = SENDER_EMAIL
    msg["To"] = TO_EMAIL

    print(f"Sending email to {TO_EMAIL} ...")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, TO_EMAIL, msg.as_string())
        print(f"Email sent to {TO_EMAIL}")
        return True
    except Exception as e:
        print(f"Email failed: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Alert on cigarking.com deals")
    parser.add_argument("--min-discount", type=float, default=40,
                        help="minimum %% off (default 40)")
    parser.add_argument("--max-price", type=float, default=125.0,
                        help="maximum price (default 125)")
    parser.add_argument("--alt-min-discount", type=float, default=25,
                        help="alternate rule: minimum %% off (default 25)")
    parser.add_argument("--alt-min-price", type=float, default=100.0,
                        help="alternate rule: lowest price (default 100)")
    parser.add_argument("--alt-max-price", type=float, default=125.0,
                        help="alternate rule: highest price (default 125)")
    parser.add_argument("--categories", nargs="+", default=None,
                        help="scan only these category slugs, e.g. weekly-deals "
                             "(default: discover deal categories from the sitemap)")
    parser.add_argument("--full-catalog", action="store_true",
                        help="scan every category in the sitemap, not just "
                             "deal pages (much slower)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="limit pages per category (for testing)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds between requests")
    parser.add_argument("--history-file", type=Path, default=HISTORY_FILE)
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape + filter only, no email, no history write")
    args = parser.parse_args()

    session = requests.Session()

    if args.categories:
        categories = [f"{BASE_URL}/{c.strip('/')}/" for c in args.categories]
    else:
        print("Reading category sitemap ...")
        all_cats = category_urls(session)
        print(f"  {len(all_cats)} categories in sitemap")
        categories = pick_categories(all_cats, args.full_catalog)
    print(f"Scanning {len(categories)} categor{'y' if len(categories) == 1 else 'ies'}"
          + (" (full catalog)" if args.full_catalog else " (deal pages)") + "\n")

    matches, examined = scrape_deals(
        session,
        categories,
        rules={
            "min_discount": args.min_discount,
            "max_price": args.max_price,
            "alt_min_discount": args.alt_min_discount,
            "alt_min_price": args.alt_min_price,
            "alt_max_price": args.alt_max_price,
        },
        max_pages=args.max_pages,
        delay=args.delay,
    )
    print(f"\n{examined} item(s) examined, {len(matches)} meet the "
          f"discount/price criteria this run.")

    if examined == 0:
        # Nothing parsed at all: a block or a site change, not an empty store.
        print("Nothing was read from the site. Check the messages above.")
        raise SystemExit(1)

    history = load_history(args.history_file)

    def is_new(item: dict) -> bool:
        last_price = history.get(item["url"], "__unseen__")
        if last_price == "__unseen__":
            return True  # never notified about this item before
        if last_price is None:
            return False  # old-format entry with no price; treat as seen
        return item["price"] < last_price  # got even cheaper since last email

    new_items = [m for m in matches if is_new(m)]

    if not new_items:
        print("No new items since last run. Nothing to email.")
        return

    print(f"{len(new_items)} of those are new (first-seen, or a deeper discount than before):")
    for item in new_items:
        print(f"  - {item['name']} (${item['price']:.2f}, "
              f"{item['discount_pct']:g}% off) {item['url']}")

    if args.dry_run:
        print("\n--dry-run set: skipping email and history update.")
        return

    if not send_email(new_items):
        return  # don't mark as notified if the email never went out

    for item in new_items:
        history[item["url"]] = item["price"]
    save_history(args.history_file, history)
    print(f"History updated: {args.history_file}")


if __name__ == "__main__":
    main()
