#!/usr/bin/env python3
"""
pcb_deal_alert.py — watch perfectcigarblend.com for deals and email new ones.

Each run:
  1. Crawls the paginated cigar catalog.
  2. Parses each product's MSRP, sale price, and "you save X%" discount.
  3. Keeps items where discount >= --min-discount AND price <= --max-price.
  4. Diffs against sent_history.json (product URLs already emailed).
  5. Emails only the NEW matches (if any) and updates the history file.

Nothing is emailed if there's nothing new — the script just exits quietly,
which is what makes it safe to run on a schedule (cron / GitHub Actions).

Setup
-----
    pip install requests beautifulsoup4 --break-system-packages

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set as a GitHub Actions secret when run on a
schedule). Sender/recipient are set as constants below.

    export GMAIL_PASSWORD='app-specific-password'

Usage
-----
    python pcb_deal_alert.py                      # normal run
    python pcb_deal_alert.py --min-discount 40 --max-price 125
    python pcb_deal_alert.py --dry-run             # scrape + filter, no email, no history write
    python pcb_deal_alert.py --max-pages 5          # only scan first 5 pages (quick test)
"""

import argparse
import json
import os
import re
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://perfectcigarblend.com"
CATALOG_URL_TMPL = f"{BASE_URL}/product-category/cigars/page/{{page}}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

HISTORY_FILE = Path(__file__).parent / "pcb_sent_history.json"

#  EMAIL CONFIG (matches the pattern used elsewhere in this repo)
SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
SAVE_RE = re.compile(r"You save(?: up to)?\s*(\d+)%")


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def get_soup(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _parse_product(li) -> dict | None:
    link_tag = li.select_one(
        "a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link"
    )
    name_tag = li.select_one("h2, h3, .woocommerce-loop-product__title")
    if not link_tag or not name_tag:
        return None

    text = li.get_text(" ", strip=True)

    prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(text)]
    if not prices:
        return None
    # Lowest price mentioned is the current/sale starting price (msrp is
    # always listed first and is higher, so min() is a safe way to get the
    # cheapest current variant price without needing to split MSRP vs sale).
    current_price = min(prices)

    save_match = SAVE_RE.search(text)
    discount_pct = int(save_match.group(1)) if save_match else 0

    li_classes = li.get("class", [])
    out_of_stock = (
        "outofstock" in li_classes
        or li.select_one(".out-of-stock, .outofstock") is not None
    )

    return {
        "name": name_tag.get_text(strip=True),
        "url": urljoin(BASE_URL, link_tag["href"]),
        "price": current_price,
        "discount_pct": discount_pct,
        "in_stock": not out_of_stock,
    }


def scrape_deals(
    session: requests.Session,
    min_discount: int,
    max_price: float,
    max_pages: int | None = None,
    delay: float = 1.5,
) -> list[dict]:
    matches = []
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break

        url = CATALOG_URL_TMPL.format(page=page)
        print(f"Scanning page {page}: {url}")
        try:
            soup = get_soup(url, session)
        except requests.RequestException as e:
            print(f"  failed: {e}")
            break

        products = [p for li in soup.select("li.product") if (p := _parse_product(li))]
        if not products:
            print("  no products found, stopping.")
            break

        for p in products:
            if p["in_stock"] and p["discount_pct"] >= min_discount and p["price"] <= max_price:
                matches.append(p)

        page += 1
        time.sleep(delay)

    return matches


# --------------------------------------------------------------------------
# History (dedup across runs)
# --------------------------------------------------------------------------

def load_history(path: Path) -> dict:
    """Returns {url: last_notified_price}."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Back-compat: older versions stored a flat list of URLs with no price.
    if isinstance(data, list):
        return {url: None for url in data}
    return data


def save_history(path: Path, history: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(new_items: list[dict]) -> bool:
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. The changes are still in the history file.")
        return False

    lines = [
        f"- {item['name']} - ${item['price']:.2f} ({item['discount_pct']}% off)\n  {item['url']}"
        for item in new_items
    ]
    body = f"Found {len(new_items)} new cigar deal(s):\n\n" + "\n\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"{len(new_items)} new cigar deal(s) on Perfect Cigar Blend"
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
    parser = argparse.ArgumentParser(description="Alert on perfectcigarblend.com deals")
    parser.add_argument("--min-discount", type=int, default=40, help="minimum %% off (default 40)")
    parser.add_argument("--max-price", type=float, default=125.0, help="maximum price (default 125)")
    parser.add_argument("--max-pages", type=int, default=None, help="limit pages scanned (for testing)")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between page requests")
    parser.add_argument("--history-file", type=Path, default=HISTORY_FILE)
    parser.add_argument("--dry-run", action="store_true", help="scrape + filter only, no email, no history write")
    args = parser.parse_args()

    session = requests.Session()

    matches = scrape_deals(
        session,
        min_discount=args.min_discount,
        max_price=args.max_price,
        max_pages=args.max_pages,
        delay=args.delay,
    )
    print(f"\n{len(matches)} item(s) meet the discount/price criteria this run.")

    history = load_history(args.history_file)

    def is_new(item: dict) -> bool:
        last_price = history.get(item["url"], "__unseen__")
        if last_price == "__unseen__":
            return True  # never notified about this URL before
        if last_price is None:
            return False  # old-format entry with no price on record; treat as already seen
        return item["price"] < last_price  # got even cheaper since last notification

    new_items = [m for m in matches if is_new(m)]

    if not new_items:
        print("No new items since last run. Nothing to email.")
        return

    print(f"{len(new_items)} of those are new (first-seen, or a deeper discount than before):")
    for item in new_items:
        print(f"  - {item['name']} (${item['price']:.2f}, {item['discount_pct']}% off) {item['url']}")

    if args.dry_run:
        print("\n--dry-run set: skipping email and history update.")
        return

    sent = send_email(new_items)
    if not sent:
        return  # don't mark these as notified if the email never actually went out

    for item in new_items:
        history[item["url"]] = item["price"]
    save_history(args.history_file, history)
    print(f"History updated: {args.history_file}")


if __name__ == "__main__":
    main()
