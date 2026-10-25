"""
Shared pieces every deal alert needs, whatever platform the store runs on:

    fetch()        HTTP GET with retries and polite backoff
    Rules          the discount / price filter (with an optional second rule)
    History        the <store>_sent_history.json file: what's been emailed
    send_email()   the Gmail alert
    run()          the command line every store script shares

A store plugs in by providing a Store object (see bigcommerce.py and
shopify.py) with:

    name                 shown in logs and the email subject
    history_file         Path of its sent-history JSON
    defaults             dict of default CLI values, e.g. {"min_discount": 33}
    add_arguments(p)     add store-specific CLI flags (optional)
    scan(session, args)  return (matching-or-not items, number examined)

Each item is a dict with at least:
    name, url, price, discount_pct, in_stock
and optionally:
    size        size/variant label to show after the name
    ref         the price it's discounted from
    ref_label   what to call ref in the email ("was", "MSRP")
    from_price  cheapest size, for listings priced as a range
"""

import argparse
import json
import os
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests

SENDER_EMAIL = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")  # set in GitHub Secrets
TO_EMAIL = "peltz.chris@gmail.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url, session, params=None, accept=HTML_ACCEPT, timeout=25):
    """
    GET a URL. Returns the Response on HTTP 200, or None on failure (with
    the reason printed). Retries network errors, and backs off on 429/503.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": accept,
               "Accept-Language": "en-US,en;q=0.9"}
    for attempt in range(3):
        try:
            resp = session.get(url, headers=headers, params=params, timeout=timeout)
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
        if resp.status_code == 404:
            print(f"  HTTP 404 - {url} doesn't exist (renamed or removed?)")
            return None
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}")
            return None
        return resp
    return None


def discount(price, ref):
    """Percent off, 1 decimal place. 0 when there's no real markdown."""
    if price is None or not ref or ref <= price:
        return 0
    return round((ref - price) / ref * 100, 1)


# --------------------------------------------------------------------------
# Deal rules
# --------------------------------------------------------------------------

class Rules:
    """
    An item qualifies if it's in stock and passes EITHER rule:
      main:  discount >= min_discount AND price <= max_price
      alt:   discount >= alt_min_discount AND
             alt_min_price <= price <= alt_max_price
    The alt rule is off unless alt_min_discount is set.
    """

    def __init__(self, min_discount, max_price, alt_min_discount=None,
                 alt_min_price=None, alt_max_price=None):
        self.min_discount = min_discount
        self.max_price = max_price
        self.alt_min_discount = alt_min_discount
        self.alt_min_price = alt_min_price if alt_min_price is not None else 0
        self.alt_max_price = alt_max_price if alt_max_price is not None else max_price

    def qualifies(self, item):
        if not item["in_stock"]:
            return False
        pct, price = item["discount_pct"], item["price"]
        if pct >= self.min_discount and price <= self.max_price:
            return True
        if self.alt_min_discount is None:
            return False
        return (pct >= self.alt_min_discount
                and self.alt_min_price <= price <= self.alt_max_price)

    def describe(self):
        text = f"{self.min_discount:g}%+ off at ${self.max_price:g} or less"
        if self.alt_min_discount is not None:
            text += (f", or {self.alt_min_discount:g}%+ off between "
                     f"${self.alt_min_price:g} and ${self.alt_max_price:g}")
        return text


# --------------------------------------------------------------------------
# Sent history
# --------------------------------------------------------------------------

class History:
    """{url: last-emailed price}. An item is new if never emailed, or cheaper now."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.existed = self.path.exists()
        if not self.existed:
            print(f"No {self.path.name} yet (first run); starting a new one.")
            return
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return  # an empty file, e.g. created by hand on GitHub
        try:
            loaded = json.loads(text)
        except ValueError:
            # Don't let a damaged file stop the alerts; start over instead.
            print(f"WARNING: {self.path.name} isn't valid JSON; starting a new one.")
            return
        # back-compat: an older flat list of URLs with no prices
        self.data = ({u: None for u in loaded} if isinstance(loaded, list)
                     else loaded or {})

    def is_new(self, item):
        if item["url"] not in self.data:
            return True
        last = self.data[item["url"]]
        if last is None:
            return False  # old-format entry with no price; treat as seen
        return item["price"] < last

    def record(self, items):
        for item in items:
            self.data[item["url"]] = item["price"]

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def describe_item(item):
    size = f" - {item['size']}" if item.get("size") else ""
    ref = (f" ({item.get('ref_label', 'was')} ${item['ref']:.2f})"
           if item.get("ref") else "")
    sizes = (f"\n  largest size; smaller packs from ${item['from_price']:.2f}"
             if item.get("from_price") else "")
    return (f"{item['name']}{size}\n"
            f"  ${item['price']:.2f}{ref}, {item['discount_pct']:g}% off{sizes}\n"
            f"  {item['url']}")


def send_email(store_name, items):
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. History not updated, so these will retry next run.")
        return False

    ordered = sorted(items, key=lambda x: (-x["discount_pct"], x["price"]))
    body = (f"Found {len(items)} new cigar deal(s) on {store_name}:\n\n"
            + "\n\n".join(describe_item(i) for i in ordered))

    msg = MIMEText(body)
    msg["Subject"] = f"{len(items)} new cigar deal(s) on {store_name}"
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
# Command line
# --------------------------------------------------------------------------

def build_parser(store):
    d = {"min_discount": 40, "max_price": 125.0, "alt_min_discount": None,
         "alt_min_price": None, "alt_max_price": None, "delay": 1.5}
    d.update(getattr(store, "defaults", {}))

    p = argparse.ArgumentParser(description=f"Alert on {store.name} deals")
    p.add_argument("--min-discount", type=float, default=d["min_discount"],
                   help=f"minimum %% off (default {d['min_discount']:g})")
    p.add_argument("--max-price", type=float, default=d["max_price"],
                   help=f"maximum price (default {d['max_price']:g})")
    p.add_argument("--alt-min-discount", type=float, default=d["alt_min_discount"],
                   help="optional second rule: minimum %% off "
                        f"(default {d['alt_min_discount'] or 'off'})")
    p.add_argument("--alt-min-price", type=float, default=d["alt_min_price"],
                   help="second rule: lowest price")
    p.add_argument("--alt-max-price", type=float, default=d["alt_max_price"],
                   help="second rule: highest price")
    p.add_argument("--max-pages", type=int, default=None,
                   help="limit pages per category/collection (for testing)")
    p.add_argument("--delay", type=float, default=d["delay"],
                   help="seconds between requests")
    p.add_argument("--history-file", type=Path, default=store.history_file)
    p.add_argument("--dry-run", action="store_true",
                   help="scrape + filter only, no email, no history write")
    if hasattr(store, "add_arguments"):
        store.add_arguments(p)
    return p


def run(store, argv=None):
    args = build_parser(store).parse_args(argv)
    rules = Rules(args.min_discount, args.max_price, args.alt_min_discount,
                  args.alt_min_price, args.alt_max_price)
    print(f"{store.name}: looking for in-stock items {rules.describe()}\n")

    session = requests.Session()
    items, examined = store.scan(session, args)
    matches = [i for i in items if rules.qualifies(i)]
    print(f"\n{examined} item(s) examined, {len(matches)} meet the "
          f"discount/price criteria this run.")

    if examined == 0:
        # Nothing parsed at all: a block or a site change, not an empty store.
        print("Nothing was read from the site. Check the messages above.")
        raise SystemExit(1)

    history = History(args.history_file)
    if not history.existed and not args.dry_run:
        history.save()  # first run: create the file so the workflow can commit it
    new_items = [m for m in matches if history.is_new(m)]
    if not new_items:
        print("No new items since last run. Nothing to email.")
        return

    print(f"{len(new_items)} of those are new (first-seen, or cheaper than before):")
    for item in new_items:
        size = f" [{item['size']}]" if item.get("size") else ""
        print(f"  - {item['name']}{size} (${item['price']:.2f}, "
              f"{item['discount_pct']:g}% off) {item['url']}")

    if args.dry_run:
        print("\n--dry-run set: skipping email and history update.")
        return
    if not send_email(store.name, new_items):
        return  # don't mark as notified if the email never went out

    history.record(new_items)
    history.save()
    print(f"History updated: {args.history_file}")
