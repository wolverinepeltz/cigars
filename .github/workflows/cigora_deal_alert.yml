#!/usr/bin/env python3
"""
cigora_deal_alert.py — watch cigora.com for deals and email new ones.

Cigora runs on Salesforce Commerce Cloud (SFCC/Demandware), so there is no
public JSON product API. This script crawls the HTML category listings
(24 products per page, ?start=N&sz=24 pagination) and:

  1. Crawls the paginated cigar catalog.
  2. Parses each product's list price, sale price, and discount %.
  3. Keeps items where discount >= --min-discount AND price <= --max-price.
  4. Diffs against cigora_sent_history.json (product URLs already emailed).
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
    python cigora_deal_alert.py                      # normal run
    python cigora_deal_alert.py --min-discount 40 --max-price 125
    python cigora_deal_alert.py --dry-run            # scrape + filter, no email, no history write
    python cigora_deal_alert.py --max-pages 5        # only scan first 5 pages (quick test)
    python cigora_deal_alert.py --category samplers  # crawl /category/samplers/ instead

Notes
-----
Cigora bot-protects aggressively: plain requests from some IPs get HTTP 403
(the same fetch works fine from a residential IP / real browser). If every
page 403s, the IP is blocked and the script exits with an error — the same
code often works from a home connection or a different Actions runner.
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

BASE_URL = "https://www.cigora.com"
CATEGORY = "cigars"                      # /category/<name>/
PAGE_SIZE = 24                           # products per listing page
LISTING_URL_TMPL = (BASE_URL + "/category/{category}/"
                   "?start={start}&sz=" + str(PAGE_SIZE))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}

HISTORY_FILE = Path(__file__).parent / "cigora_sent_history.json"

#  EMAIL CONFIG (matches the pattern used elsewhere in this repo)
SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

PRICE_RE = re.compile(r"\$([\d,]+\.\d{2})")
SAVE_RE = re.compile(r"(?:you\s+)?save(?:\s+up\s+to)?\s*(\d+)\s*%", re.I)
OFF_RE = re.compile(r"(\d+)\s*%\s*off", re.I)

# SFCC product URL pattern: /product/<name>/<product-id>.html
PRODUCT_URL_RE = re.compile(r"^https?://(?:www\.)?cigora\.com/product/[^?#]+\.html")

OUT_OF_STOCK_RES = [
    re.compile(r"\bout of stock\b", re.I),
    re.compile(r"\bsold out\b", re.I),
    re.compile(r"\bnot available\b", re.I),
]


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

def get_soup(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=25)
    if resp.status_code == 403:
        raise requests.RequestException(
            f"HTTP 403 from {url} — this IP is bot-blocked. "
            "The same code often works from a home connection."
        )
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _card_prices(card) -> tuple[float | None, float | None]:
    """
    Returns (sale_price, list_price).

    Cigora's tile markup carries exact numeric values in `content`
    attributes:
      sale: <span class="sales"><span class="value" content="49.99">
      list: <del><span class="strike-through list"><span class="value"
             content="56.99">
    Falls back to SFCC-generic selectors, then to min/max of every
    $ amount in the card.
    """
    def content_of(selector):
        el = card.select_one(selector)
        if el and el.has_attr("content"):
            try:
                return float(el["content"])
            except (TypeError, ValueError):
                pass
        return None

    sale = (content_of(".sales .value")
            or content_of(".price .sales .value"))
    list_p = (content_of(".strike-through .value")
              or content_of("del .value")
              or content_of(".list .value"))

    if sale is None or list_p is None:
        # Fallback: every $ amount in the card.
        amounts = [float(p.replace(",", ""))
                   for p in PRICE_RE.findall(card.get_text(" ", strip=True))]
        if amounts:
            lo, hi = min(amounts), max(amounts)
            sale = sale if sale is not None else lo
            if list_p is None and hi > sale:
                list_p = hi
    return sale, list_p


def _extract_products(soup: BeautifulSoup) -> list[dict]:
    """
    Extracts one record per div.product tile.

    Tile structure (verified against the live site):
      div.product[data-pid]                      <- one tile
        a[href=/product/<slug>/<id>.html]        <- product link
        span.product-tile-product-name           <- name
        span.product-tile-badges                 <- "Sale" badge (no % given)
        .sales .value[content]                   <- sale price
        del .strike-through .value[content]      <- list price
      "SOLD OUT" text marks unavailable tiles.
    """
    products = []
    seen_urls = set()

    for tile in soup.select("div.product"):
        link = tile.select_one("a[href]")
        if not link:
            continue
        href = urljoin(BASE_URL, link["href"]).split("?")[0].split("#")[0]
        if not PRODUCT_URL_RE.match(href) or href in seen_urls:
            continue

        name_el = tile.select_one(".product-tile-product-name")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        sale_price, list_price = _card_prices(tile)
        if sale_price is None:
            continue

        if list_price and list_price > sale_price:
            discount_pct = round((list_price - sale_price) / list_price * 100, 1)
        else:
            discount_pct = 0

        tile_text = tile.get_text(" ", strip=True)
        out_of_stock = any(rx.search(tile_text) for rx in OUT_OF_STOCK_RES)

        seen_urls.add(href)
        products.append({
            "name": name,
            "url": href,
            "price": sale_price,
            "list_price": list_price,
            "discount_pct": discount_pct,
            "in_stock": not out_of_stock,
        })

    return products


def scrape_deals(
    session: requests.Session,
    category: str,
    min_discount: float,
    max_price: float,
    max_pages: int | None = None,
    delay: float = 1.5,
) -> list[dict]:
    matches = []
    seen_urls = set()  # dedup ACROSS pages, not just within one page - a
                        # product can drift onto an adjacent page between
                        # requests if the site's sort order isn't fully stable
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break

        start = page * PAGE_SIZE
        url = LISTING_URL_TMPL.format(category=category, start=start)
        print(f"Scanning page {page + 1} (start={start}): {url}")
        try:
            soup = get_soup(url, session)
        except requests.RequestException as e:
            print(f"  failed: {e}")
            break

        products = _extract_products(soup)
        if not products:
            print("  no products found, stopping.")
            break

        for p in products:
            if p["url"] in seen_urls:
                continue
            seen_urls.add(p["url"])
            if (p["in_stock"]
                    and p["discount_pct"] >= min_discount
                    and p["price"] <= max_price):
                matches.append(p)

        # SFCC listings end when a page comes back short.
        if len(products) < PAGE_SIZE:
            print("  short page — end of catalog.")
            break

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

def send_email(new_items: list[dict], category: str) -> bool:
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. The changes are still in the history file.")
        return False

    lines = [
        f"- {item['name']} - ${item['price']:.2f} ({item['discount_pct']}% off)\n  {item['url']}"
        for item in new_items
    ]
    body = f"Found {len(new_items)} new cigar deal(s) on Cigora:\n\n" + "\n\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"{len(new_items)} new cigar deal(s) on Cigora"
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
    parser = argparse.ArgumentParser(description="Alert on cigora.com deals")
    parser.add_argument("--category", default=CATEGORY,
                        help="category slug under /category/ (default: cigars)")
    parser.add_argument("--min-discount", type=float, default=40,
                        help="minimum %% off (default 40)")
    parser.add_argument("--max-price", type=float, default=125.0,
                        help="maximum price (default 125)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="limit pages scanned (for testing)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="seconds between page requests")
    parser.add_argument("--history-file", type=Path, default=HISTORY_FILE)
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape + filter only, no email, no history write")
    args = parser.parse_args()

    session = requests.Session()

    matches = scrape_deals(
        session,
        category=args.category,
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

    sent = send_email(new_items, args.category)
    if not sent:
        return  # don't mark these as notified if the email never actually went out

    for item in new_items:
        history[item["url"]] = item["price"]
    save_history(args.history_file, history)
    print(f"History updated: {args.history_file}")


if __name__ == "__main__":
    main()