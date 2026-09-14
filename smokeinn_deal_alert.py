#!/usr/bin/env python3
"""
smokeinn_deal_alert.py - watch smokeinn.com's Clearance page for deals and
email new ones.

smokeinn.com runs on a different platform than perfectcigarblend.com (looks
like Miva Merchant, not WooCommerce) with its own URL scheme
(ProductName.html instead of /product/slug/), so this is a separate script
rather than reusing pcb_deal_alert.py's selectors.

Each run:
  1. Crawls the Clearance page.
  2. Parses each item's retail price, sale price, and "Save X%" discount
     (all three are printed directly on the page, no guessing needed).
  3. Keeps items where discount >= --min-discount AND price <= --max-price.
  4. Diffs against sent_history.json (url -> last-notified price). "New"
     means never seen this URL before, OR seen it but at a higher price
     than what's showing now (i.e. it got even cheaper).
  5. Emails only the NEW matches (if any) and updates the history file.

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
    python smokeinn_deal_alert.py
    python smokeinn_deal_alert.py --min-discount 40 --max-price 125
    python smokeinn_deal_alert.py --dry-run

Caveat
------
This was verified against the Clearance page's markup and text format as
fetched once during development, but not run live end-to-end against the
site (my sandbox can't reach it). It also hasn't been confirmed whether
the Clearance listing paginates beyond one page - if it turns out there's
more than what a single fetch returns, this will need a pagination pass
added, the same way the perfectcigarblend version steps through
/page/N/.
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

BASE_URL = "https://www.smokeinn.com"
CLEARANCE_URL = f"{BASE_URL}/Clearance/"
CIGAR_LIST_URL = f"{BASE_URL}/Cigar-List/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

HISTORY_FILE = Path(__file__).parent / "smokeinn_sent_history.json"

#  EMAIL CONFIG (matches the pattern used elsewhere in this repo)
SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

# Product pages on this site are ProductName.html or ProductName-SKU123.html
# directly off the root - category/info pages use a trailing slash instead,
# so the .html suffix alone is enough to tell products apart from nav links.
PRODUCT_URL_RE = re.compile(r"^https?://www\.smokeinn\.com/[^/]+\.html$")

RETAIL_RE = re.compile(r"Retail price:\s*\$([\d,]+\.\d{2})\s*\$([\d,]+\.\d{2})")
SAVE_RE = re.compile(r"Save\s*(\d+)%")

# Line pages (e.g. /Padron-1926-No-47/) and the Hot Weekly Deal page use a
# different template than Clearance: "Smoke Inn Price : $Y" always appears,
# and "Retail Price: $X | Save Z%" only appears when there's an active
# discount - no MSRP line at all means the item is just sold at full price.
SKU_PRICE_RE = re.compile(r"Smoke Inn Price\s*:?\s*\$([\d,]+\.\d{2})")
SKU_RETAIL_SAVE_RE = re.compile(r"Retail Price\s*:?\s*\$([\d,]+\.\d{2})\s*\|\s*Save\s*(\d+)%")

# Line-listing links on Cigar-List are single path segments with a trailing
# slash (e.g. /Padron-1926-No-47/), same shape as a handful of static nav
# pages, which this excludes by name. Anything else in this shape is worth
# a try - if it turns out to be a non-product page, it simply yields zero
# priced rows and costs one wasted request, never a wrong result.
LINE_URL_RE = re.compile(r"^https?://www\.smokeinn\.com/[^/]+/$")
NON_PRODUCT_PATHS = {
    "cigars", "cigar-list", "samplers", "cigar-accessories", "value-bundles",
    "about-us", "smoke-inn-rewards", "smoke-inn-collaboration-samplers",
    "brand-samplers", "showdown-samplers", "all-lighters",
    "lighters-single-flame", "lighters-multi-flame", "lighters-soft-flame",
    "lighters-table-top", "all-cutters", "cutters-guillotine",
    "cutters-punch-cutters", "cutters-v-cutters", "all-ashtrays",
    "ashtrays-1-2-finger", "ashtrays-3-4-finger", "ashtrays-ceramic",
    "ashtrays-metal", "all-cases", "cases-small", "cases-large", "si-gear",
    "the-great-smoke-store-gear", "colibri-brand", "xikar-brand",
    "miscellaneous-accessories", "cotm", "clearance", "special-offers",
    "retail-stores", "contact", "blog", "kma-highlights", "new-to-smokeinn",
    "smoke-inn-exclusive-cigars", "smoke-inn-exclusive-offerings",
    "hot-weekly-cigar-deal", "cigar-of-the-month-club",
    "coupons-and-promotional-restrictions", "privacy-statement",
    "terms-and-conditions", "accessibility-statement",
    "drew-estate-choose-your-swag-promo", "gars-for-gunners",
    "international-orders-1", "return-policy", "age-verification",
    "shipping-rates", "order-tracking",
}


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def get_soup(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Finds items by their .html product-link pattern, then climbs from the
    title link to the smallest ancestor block that stays within a single
    item (i.e. climbing further would pull in a second product's link).
    This survives markup differences without needing to guess an exact
    item container class.
    """
    products = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not PRODUCT_URL_RE.match(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue  # image-only anchor wrapping the same product link
        if href in seen_urls:
            continue

        card = a
        node = a
        for _ in range(10):
            if node.parent is None:
                break
            candidate = node.parent
            hrefs_here = {
                urljoin(BASE_URL, x["href"])
                for x in candidate.find_all("a", href=True)
                if PRODUCT_URL_RE.match(urljoin(BASE_URL, x["href"]))
            }
            if len(hrefs_here) > 1:
                break  # climbed too far, this level includes a sibling product
            card = candidate
            node = candidate

        block_text = card.get_text(" ", strip=True)

        retail_match = RETAIL_RE.search(block_text)
        if not retail_match:
            continue  # not a priced item (shouldn't happen on Clearance, but be safe)
        current_price = float(retail_match.group(2).replace(",", ""))

        save_match = SAVE_RE.search(block_text)
        discount_pct = int(save_match.group(1)) if save_match else 0

        out_of_stock = "out of stock" in block_text.lower()

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": current_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products


def scrape_deals(
    session: requests.Session,
    min_discount: int,
    max_price: float,
    url: str = CLEARANCE_URL,
) -> list[dict]:
    print(f"Scanning: {url}")
    try:
        soup = get_soup(url, session)
    except requests.RequestException as e:
        print(f"  failed: {e}")
        return []

    products = _extract_products(soup)
    print(f"  found {len(products)} priced item(s) on the page")

    matches = [
        p for p in products
        if p["in_stock"] and p["discount_pct"] >= min_discount and p["price"] <= max_price
    ]
    return matches


def _extract_line_page_products(soup: BeautifulSoup) -> list[dict]:
    """
    Parses the 'Product / Price / MSRP / Qty / Cart' table used on line
    pages (e.g. /Padron-1926-No-47/) and the Hot Weekly Deal page. Unlike
    Clearance, MSRP/Save% is OPTIONAL here - it only appears when a SKU has
    an active discount; a row with no MSRP text just means full price, so
    it gets discount_pct=0 and is filtered out downstream like any other
    non-qualifying item.
    """
    products = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not PRODUCT_URL_RE.match(href):
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        if href in seen_urls:
            continue

        card = a
        node = a
        for _ in range(10):
            if node.parent is None:
                break
            candidate = node.parent
            hrefs_here = {
                urljoin(BASE_URL, x["href"])
                for x in candidate.find_all("a", href=True)
                if PRODUCT_URL_RE.match(urljoin(BASE_URL, x["href"]))
            }
            if len(hrefs_here) > 1:
                break
            card = candidate
            node = candidate

        block_text = card.get_text(" ", strip=True)

        price_match = SKU_PRICE_RE.search(block_text)
        if not price_match:
            continue  # not a priced row (e.g. picked up a stray nav link)
        current_price = float(price_match.group(1).replace(",", ""))

        retail_save_match = SKU_RETAIL_SAVE_RE.search(block_text)
        discount_pct = int(retail_save_match.group(2)) if retail_save_match else 0

        out_of_stock = "out of stock" in block_text.lower()

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": current_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products


def scrape_brand_lines(session: requests.Session, max_lines: int | None = None) -> list[str]:
    """Returns line-page URLs found on the Big List of Brands page."""
    print(f"Fetching brand/line list: {CIGAR_LIST_URL}")
    try:
        soup = get_soup(CIGAR_LIST_URL, session)
    except requests.RequestException as e:
        print(f"  failed: {e}")
        return []

    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE_URL, a["href"])
        if not LINE_URL_RE.match(href):
            continue
        path_segment = href.rstrip("/").rsplit("/", 1)[-1].lower()
        if path_segment in NON_PRODUCT_PATHS:
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
        if max_lines is not None and len(urls) >= max_lines:
            break

    print(f"  found {len(urls)} candidate line page(s)")
    return urls


def scrape_full_catalog(
    session: requests.Session,
    min_discount: int,
    max_price: float,
    max_lines: int | None = None,
    delay: float = 1.0,
) -> list[dict]:
    line_urls = scrape_brand_lines(session, max_lines=max_lines)

    matches = []
    seen_urls = set()
    for i, url in enumerate(line_urls, 1):
        if i % 50 == 0 or i == len(line_urls):
            print(f"  ... {i}/{len(line_urls)} line pages scanned")
        try:
            soup = get_soup(url, session)
        except requests.RequestException as e:
            print(f"  {url} failed: {e}")
            time.sleep(delay)
            continue

        for p in _extract_line_page_products(soup):
            if p["url"] in seen_urls:
                continue
            if p["in_stock"] and p["discount_pct"] >= min_discount and p["price"] <= max_price:
                matches.append(p)
                seen_urls.add(p["url"])

        time.sleep(delay)

    return matches


# --------------------------------------------------------------------------
# History (dedup across runs, re-notify on a deeper discount)
# --------------------------------------------------------------------------

def load_history(path: Path) -> dict:
    """Returns {url: last_notified_price}."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # back-compat with an older flat-list format
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
    body = f"Found {len(new_items)} new cigar deal(s) on Smoke Inn:\n\n" + "\n\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"{len(new_items)} new cigar deal(s) on Smoke Inn"
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
    parser = argparse.ArgumentParser(description="Alert on smokeinn.com deals")
    parser.add_argument("--min-discount", type=int, default=40, help="minimum %% off (default 40)")
    parser.add_argument("--max-price", type=float, default=125.0, help="maximum price (default 125)")
    parser.add_argument("--url", type=str, default=CLEARANCE_URL, help="override the Clearance page to scan")
    parser.add_argument("--full-catalog", action="store_true",
                         help="also crawl every brand/line page from the Big List of Brands "
                              "(~1000 pages - much slower, and each line's discount status can "
                              "change independently, so this finds deals Clearance alone misses)")
    parser.add_argument("--max-lines", type=int, default=None,
                         help="full-catalog mode: cap how many line pages to scan (for a quick test run)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests in full-catalog mode")
    parser.add_argument("--history-file", type=Path, default=HISTORY_FILE)
    parser.add_argument("--dry-run", action="store_true", help="scrape + filter only, no email, no history write")
    args = parser.parse_args()

    session = requests.Session()

    matches = scrape_deals(session, min_discount=args.min_discount, max_price=args.max_price, url=args.url)
    print(f"Clearance: {len(matches)} item(s) meet the discount/price criteria.")

    if args.full_catalog:
        print("\nStarting full-catalog crawl (this takes a while) ...")
        catalog_matches = scrape_full_catalog(
            session,
            min_discount=args.min_discount,
            max_price=args.max_price,
            max_lines=args.max_lines,
            delay=args.delay,
        )
        print(f"Full catalog: {len(catalog_matches)} additional item(s) meet the criteria.")

        seen = {m["url"] for m in matches}
        for m in catalog_matches:
            if m["url"] not in seen:
                matches.append(m)
                seen.add(m["url"])

    print(f"\n{len(matches)} total item(s) meet the discount/price criteria this run.")

    history = load_history(args.history_file)

    def is_new(item: dict) -> bool:
        last_price = history.get(item["url"], "__unseen__")
        if last_price == "__unseen__":
            return True
        if last_price is None:
            return False
        return item["price"] < last_price

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
        return

    for item in new_items:
        history[item["url"]] = item["price"]
    save_history(args.history_file, history)
    print(f"History updated: {args.history_file}")


if __name__ == "__main__":
    main()
