#!/usr/bin/env python3
"""
gtcigars_deal_alert.py - watch gtcigars.com for deals and email new ones.

GT Cigars runs on Shopify, so unlike the HTML scrapers elsewhere in this
repo it reads Shopify's public collection feed:

    /collections/<handle>/products.json?limit=250&page=N

That feed carries exact numbers for every size (variant) of every product:
    price             -> what GT charges now
    compare_at_price  -> the MSRP it's marked down from
    available         -> in stock or not
so nothing has to be parsed out of page text, and markup changes can't
break it.

Each run:
  1. Pulls every product in the chosen collections (all cigars plus the
     daily / weekly / monthly deal collections by default).
  2. Works out each size's discount from price vs compare_at_price.
  3. Keeps sizes where discount >= --min-discount AND price <= --max-price
     AND in stock.
  4. Diffs against gtcigars_sent_history.json (link -> last-notified price).
     "New" means never seen before, OR seen but now cheaper than when it
     was last emailed.
  5. Emails only the NEW matches (if any) and updates the history file.

Nothing is emailed if there's nothing new.

Each deal is tracked per size, not per product, and the link in the email
opens the product page with that exact size pre-selected (?variant=<id>).

Pack counts: GT's variants are named by size only (e.g. "Toro (6" X 52)")
and the listings are boxes. Pack size isn't published in the feed, so
there's no pack filter here, unlike hilands_crawl.py.

Setup
-----
    pip install requests --break-system-packages

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set as a GitHub Actions secret when run on a
schedule). Sender/recipient are set as constants below.

    export GMAIL_PASSWORD='app-specific-password'

Usage
-----
    python gtcigars_deal_alert.py
    python gtcigars_deal_alert.py --min-discount 40 --max-price 125
    python gtcigars_deal_alert.py --dry-run
    python gtcigars_deal_alert.py --collections daily-deals weekly-deals
    python gtcigars_deal_alert.py --max-pages 1          # quick test
"""

import argparse
import json
import os
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_URL = "https://gtcigars.com"
FEED_URL_TMPL = BASE_URL + "/collections/{handle}/products.json"
PAGE_SIZE = 250  # Shopify's max per page for products.json

# shop-all-cigars is the full premium catalog; the deal collections are
# included too in case a promo item isn't filed under shop-all. Products
# that show up in more than one are de-duplicated by variant id.
DEFAULT_COLLECTIONS = [
    "shop-all-cigars",
    "daily-deals",
    "weekly-deals",
    "monthly-deals",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

HISTORY_FILE = Path(__file__).parent / "gtcigars_sent_history.json"

#  EMAIL CONFIG (matches the pattern used elsewhere in this repo)
SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def get_json(url: str, session: requests.Session, params: dict) -> list | None:
    """
    Returns the 'products' list for one feed page, or None on failure.
    Shopify answers bursts with HTTP 429, so that gets a backoff + retry.
    """
    for attempt in range(3):
        try:
            resp = session.get(url, headers=HEADERS, params=params, timeout=25)
        except requests.RequestException as e:
            print(f"  request failed ({type(e).__name__}: {e})")
            time.sleep(3 * (attempt + 1))
            continue

        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"  HTTP 429 (rate limited), waiting {wait}s ...")
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            print(f"  HTTP 404 - collection doesn't exist (renamed or removed?)")
            return None
        if resp.status_code == 403:
            print("  HTTP 403 - this IP is blocked. The same code often "
                  "works from a home connection.")
            return None
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}")
            return None

        try:
            return resp.json().get("products", [])
        except ValueError:
            print("  response wasn't JSON (a bot-check page, maybe)")
            return None
    return None


def to_float(raw) -> float | None:
    """Shopify sends prices as strings like "79.99", or null."""
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def variant_rows(product: dict) -> list[dict]:
    """One row per size of one product."""
    handle = product.get("handle", "")
    name = (product.get("title") or "").strip()
    variants = product.get("variants") or []
    rows = []

    for v in variants:
        price = to_float(v.get("price"))
        if price is None:
            continue
        msrp = to_float(v.get("compare_at_price"))

        # compare_at below the price shows up occasionally in GT's data
        # (stale MSRP field); that's not a discount, so treat it as 0.
        if msrp and msrp > price:
            discount_pct = round((msrp - price) / msrp * 100, 1)
        else:
            discount_pct = 0

        size = (v.get("title") or "").strip()
        if size.lower() == "default title":  # Shopify's name for "no options"
            size = ""

        rows.append({
            "name": name,
            "size": size,
            "price": price,
            "msrp": msrp,
            "discount_pct": discount_pct,
            "in_stock": bool(v.get("available")),
            "variant_id": v.get("id"),
            "url": f"{BASE_URL}/products/{handle}?variant={v.get('id')}",
        })
    return rows


def scrape_collection(
    session: requests.Session,
    handle: str,
    max_pages: int | None,
    delay: float,
) -> list[dict]:
    """Every size of every product in one collection."""
    url = FEED_URL_TMPL.format(handle=handle)
    rows = []
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break
        print(f"Scanning {handle} page {page}: {url}?limit={PAGE_SIZE}&page={page}")
        products = get_json(url, session, {"limit": PAGE_SIZE, "page": page})
        if not products:
            if products is not None:
                print("  no more products.")
            break

        for p in products:
            rows.extend(variant_rows(p))
        print(f"  {len(products)} product(s)")

        if len(products) < PAGE_SIZE:
            break  # short page means that was the last one
        page += 1
        time.sleep(delay)

    return rows


def scrape_deals(
    session: requests.Session,
    collections: list[str],
    min_discount: float,
    max_price: float,
    max_pages: int | None = None,
    delay: float = 1.5,
) -> tuple[list[dict], int]:
    """Returns (matches, total sizes examined)."""
    seen_variants = set()
    matches = []
    examined = 0

    for i, handle in enumerate(collections):
        if i:
            time.sleep(delay)
        for row in scrape_collection(session, handle, max_pages, delay):
            vid = row["variant_id"]
            if vid in seen_variants:
                continue  # same item listed in more than one collection
            seen_variants.add(vid)
            examined += 1
            if (row["in_stock"]
                    and row["discount_pct"] >= min_discount
                    and row["price"] <= max_price):
                matches.append(row)

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
    size = f" - {item['size']}" if item["size"] else ""
    msrp = f" (MSRP ${item['msrp']:.2f})" if item["msrp"] else ""
    return (f"{item['name']}{size}\n"
            f"  ${item['price']:.2f}{msrp}, {item['discount_pct']:g}% off\n"
            f"  {item['url']}")


def send_email(new_items: list[dict]) -> bool:
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. History not updated, so these will retry next run.")
        return False

    ordered = sorted(new_items, key=lambda x: (-x["discount_pct"], x["price"]))
    body = (f"Found {len(new_items)} new cigar deal(s) on GT Cigars:\n\n"
            + "\n\n".join(describe(i) for i in ordered))

    msg = MIMEText(body)
    msg["Subject"] = f"{len(new_items)} new cigar deal(s) on GT Cigars"
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
    parser = argparse.ArgumentParser(description="Alert on gtcigars.com deals")
    parser.add_argument("--min-discount", type=float, default=40,
                        help="minimum %% off (default 40)")
    parser.add_argument("--max-price", type=float, default=125.0,
                        help="maximum price (default 125)")
    parser.add_argument("--collections", nargs="+", default=DEFAULT_COLLECTIONS,
                        help="collection handles to scan "
                             f"(default: {' '.join(DEFAULT_COLLECTIONS)})")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="limit feed pages per collection (for testing)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds between requests")
    parser.add_argument("--history-file", type=Path, default=HISTORY_FILE)
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape + filter only, no email, no history write")
    args = parser.parse_args()

    session = requests.Session()

    matches, examined = scrape_deals(
        session,
        collections=args.collections,
        min_discount=args.min_discount,
        max_price=args.max_price,
        max_pages=args.max_pages,
        delay=args.delay,
    )
    print(f"\n{examined} size(s) examined, {len(matches)} meet the "
          f"discount/price criteria this run.")

    if examined == 0:
        # Every collection failed or came back empty. That's a block or a
        # site change, not an empty store, so say so loudly in the Actions log.
        print("Nothing was read from the site. Check the messages above.")
        raise SystemExit(1)

    history = load_history(args.history_file)

    def is_new(item: dict) -> bool:
        last_price = history.get(item["url"], "__unseen__")
        if last_price == "__unseen__":
            return True  # never notified about this size before
        if last_price is None:
            return False  # old-format entry with no price; treat as seen
        return item["price"] < last_price  # got even cheaper since last email

    new_items = [m for m in matches if is_new(m)]

    if not new_items:
        print("No new items since last run. Nothing to email.")
        return

    print(f"{len(new_items)} of those are new (first-seen, or a deeper discount than before):")
    for item in new_items:
        size = f" [{item['size']}]" if item["size"] else ""
        print(f"  - {item['name']}{size} (${item['price']:.2f}, "
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
