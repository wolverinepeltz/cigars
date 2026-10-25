"""
Shared pieces every deal alert needs, whatever platform the store runs on:

    fetch()        HTTP GET with retries and polite backoff
    Rules          the discount / price filter (with an optional second rule)
    History        the <store>_sent_history.json file: what's been emailed,
                   and which deals are currently live
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

DISCOUNT_STEP = 1.0   # % points the discount must rise to count as a change
PARTIAL_RUN = 0.5     # a run that sees under half of last run's items is partial
META_KEY = "_meta"    # run bookkeeping stored alongside the deals


class History:
    """
    The <store>_sent_history.json file. One entry per deal URL:

        {"price": 89.95, "discount": 40.0, "active": true}

    price, discount  what the last email said about this deal
    active           whether it qualified on the latest run

    A deal is emailed when it's new, cheaper than last emailed, its discount
    rose by DISCOUNT_STEP points or more, or it's back after not qualifying.

    Older files stored just {url: price} (or a list of URLs); those load as
    active deals with no known discount, and are upgraded on the next save.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.meta = {}
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
        if isinstance(loaded, list):  # oldest format: just URLs
            loaded = {u: None for u in loaded}
        loaded = loaded or {}
        self.meta = loaded.pop(META_KEY, None) or {}
        for url, entry in loaded.items():
            if isinstance(entry, dict):
                self.data[url] = {"price": entry.get("price"),
                                  "discount": entry.get("discount"),
                                  "active": entry.get("active", True)}
            else:  # older format: the last price, or None
                self.data[url] = {"price": entry, "discount": None, "active": True}

    def change(self, item):
        """Why this qualifying item should be emailed, or None if it shouldn't."""
        entry = self.data.get(item["url"])
        if entry is None:
            return "new"
        last_price, last_pct = entry["price"], entry["discount"]
        if last_price is None:
            return None  # oldest format: seen, nothing to compare against
        reasons = []
        if not entry["active"]:
            reasons.append("back")
        if item["price"] < last_price - 0.005:
            reasons.append(f"price dropped from ${last_price:.2f}")
        elif (last_pct is not None
              and item["discount_pct"] >= last_pct + DISCOUNT_STEP):
            reasons.append(f"discount up from {last_pct:g}%")
        return ", ".join(reasons) or None

    def is_partial(self, examined):
        last = self.meta.get("last_examined")
        return bool(last) and examined < last * PARTIAL_RUN

    def update(self, emailed, qualifying, examined):
        """
        Record what was emailed, and which deals qualify right now.
        Deals that stopped qualifying are marked inactive (so they can come
        "back"), unless this run looks partial: a blocked or failed page
        would otherwise make everything on it look "back" next time.
        """
        partial = self.is_partial(examined)
        for item in emailed:
            self.data[item["url"]] = {"price": item["price"],
                                      "discount": item["discount_pct"],
                                      "active": True}
        for item in qualifying:  # deals live right now, emailed or not
            if item["url"] in self.data:
                self.data[item["url"]]["active"] = True
        if partial:
            print(f"Only {examined} item(s) read vs {self.meta['last_examined']} "
                  f"last run; looks partial, so no deals are marked inactive.")
            return
        live = {i["url"] for i in qualifying}
        dropped = [u for u, e in self.data.items() if e["active"] and u not in live]
        for url in dropped:
            self.data[url]["active"] = False
        if dropped:
            print(f"{len(dropped)} previously emailed deal(s) no longer qualify; "
                  f"they'll be emailed again if they come back.")
        self.meta["last_examined"] = examined

    def save(self):
        out = dict(sorted(self.data.items()))
        if self.meta:
            out = {META_KEY: self.meta, **out}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def describe_item(item):
    size = f" - {item['size']}" if item.get("size") else ""
    ref = (f" ({item.get('ref_label', 'was')} ${item['ref']:.2f})"
           if item.get("ref") else "")
    sizes = (f"\n  largest size; smaller packs from ${item['from_price']:.2f}"
             if item.get("from_price") else "")
    change = item.get("change")
    why = f"[{'NEW' if change == 'new' else change}] " if change else ""
    return (f"{why}{item['name']}{size}\n"
            f"  ${item['price']:.2f}{ref}, {item['discount_pct']:g}% off{sizes}\n"
            f"  {item['url']}")


def change_kind(change):
    """Main reason, for counting: new > back > price drop > discount up."""
    change = change or "new"
    if change == "new":
        return "new"
    if change.startswith("back"):
        return "back"
    if "price dropped" in change:
        return "price drop"
    return "discount up"


def summarize(items):
    """e.g. '2 new, 1 back, 3 price drops' (each deal counted once)."""
    plural = {"new": "new", "back": "back", "price drop": "price drops",
              "discount up": "discounts up"}
    counts = {}
    for i in items:
        k = change_kind(i.get("change"))
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{counts[k]} {k if counts[k] == 1 else plural[k]}"
                     for k in plural if k in counts)


def send_email(store_name, items):
    """Returns True if the mail went out."""
    if not SENDER_PASSWORD:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        print("  No email sent. History not updated, so these will retry next run.")
        return False

    ordered = sorted(items, key=lambda x: (-x["discount_pct"], x["price"]))
    summary = summarize(items)
    body = (f"{len(items)} cigar deal(s) on {store_name} ({summary}):\n\n"
            + "\n\n".join(describe_item(i) for i in ordered))

    msg = MIMEText(body)
    msg["Subject"] = f"{store_name}: {len(items)} deal(s) ({summary})"
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
    # A product can be listed on more than one page; keep its first
    # qualifying listing only.
    matches, matched_urls = [], set()
    for i in items:
        if i["url"] not in matched_urls and rules.qualifies(i):
            matches.append(i)
            matched_urls.add(i["url"])
    print(f"\n{examined} item(s) examined, {len(matches)} meet the "
          f"discount/price criteria this run.")

    if examined == 0:
        # Nothing parsed at all: a block or a site change, not an empty store.
        print("Nothing was read from the site. Check the messages above.")
        raise SystemExit(1)

    history = History(args.history_file)
    to_send = []
    for m in matches:
        why = history.change(m)
        if why:
            to_send.append({**m, "change": why})

    if to_send:
        print(f"{len(to_send)} to email ({summarize(to_send)}):")
        for item in to_send:
            size = f" [{item['size']}]" if item.get("size") else ""
            print(f"  - [{item['change']}] {item['name']}{size} "
                  f"(${item['price']:.2f}, {item['discount_pct']:g}% off) {item['url']}")
    else:
        print("Nothing new, cheaper, more discounted, or back since last run.")

    if args.dry_run:
        print("\n--dry-run set: skipping email and history update.")
        return
    if to_send and not send_email(store.name, to_send):
        return  # history untouched, so these are retried next run

    history.update(to_send, matches, examined)
    history.save()
    print(f"History updated: {args.history_file}")
