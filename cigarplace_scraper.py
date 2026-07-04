"""
Cigarplace Deals Scraper
------------------------
Crawls https://www.cigarplace.biz/cigars.html and emails every cigar
that is >= 60% off MSRP AND rated >= 4.5 stars AND our price <= $150.

Ratings come directly from Stamped.io's public widget API (the site
serves bot traffic a page variant without review markup, so the
in-page widget can't be scraped). Only products that already pass the
discount + price filters are looked up, so it's 1-2 API calls per run.

Only NEW deals (URLs not previously emailed) are sent — sent URLs are
persisted to cigarplace_sent_history.json alongside the script.

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set in GitHub Secrets).

Dependencies (installed by the GitHub Actions workflow):
    pip install playwright beautifulsoup4
    (uses the runner's preinstalled Google Chrome)
"""

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
import os

SENDER_EMAIL    = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")   # set in GitHub Secrets
TO_EMAIL        = "peltz.chris@gmail.com"

MAX_PAGES       = None    # None = all pages, or e.g. 3
MIN_DISCOUNT    = 0.40    # 60% off
MIN_RATING      = 4.5
MAX_OUR_PRICE   = 150.00
BATCH_SIZE      = 8       # parallel pages at once
GOTO_TIMEOUT_MS = 30_000  # navigation timeout
FORCE           = False   # True = ignore history, email everything found

# Stamped.io public widget credentials (also re-extracted from page 1
# at runtime in case the site rotates them)
STAMPED_API_KEY   = "pubkey-gj6eXSCyUdiY2z6sIcRU22I8b7BP0N"
STAMPED_STORE_URL = "www.cigarplace.biz"
STAMPED_BATCH     = 50    # product IDs per API call
# ══════════════════════════════════════════════════════════════════════════════

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio, csv, json, math, re, smtplib, time
import urllib.request
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ── State files ───────────────────────────────────────────────────────────────
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()

HISTORY_FILE = os.path.join(BASE_DIR, "cigarplace_sent_history.json")
OUTPUT_CSV   = os.path.join(BASE_DIR, "cigarplace_deals.csv")

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL       = "https://www.cigarplace.biz/cigars.html"
ITEMS_PER_PAGE = 48
USER_AGENT     = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# Broken/parser-blocking third parties that stall this site's load
# pipeline (secure.comodo.com fails from CI networks), plus trackers.
BLOCKED_URL_PARTS = ("comodo.com", "trustlogo", "klaviyo",
                     "googletagmanager", "google-analytics",
                     "doubleclick", "facebook")
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# ── Sent history ──────────────────────────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(sorted(history), f, indent=2)

# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_price(text):
    text = text.strip().replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def parse_products(html):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for item in soup.select("li.item.swatch-item"):
        name_el = item.select_one("h2.product-name a")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        link = "https://www.cigarplace.biz" + name_el.get("href", "")

        # Product ID: <li id="product-27885"> or data-product-id attr
        product_id = None
        li_id = item.get("id", "")
        m = re.search(r"product-(\d+)", li_id)
        if m:
            product_id = m.group(1)
        else:
            id_el = item.select_one("[data-product-id]")
            if id_el:
                product_id = id_el.get("data-product-id")

        price_box = item.select_one(".price-box")
        if not price_box:
            continue

        msrp_span  = price_box.select_one(".msrp-price")
        savings_el = item.select_one(".savings")
        msrp = parse_price(msrp_span.get_text()) if msrp_span else None

        discount_pct = 0.0
        if savings_el:
            m = re.search(r"(\d+)%", savings_el.get_text())
            if m:
                discount_pct = int(m.group(1)) / 100

        our_price = None
        if msrp and discount_pct > 0:
            our_price = round(msrp * (1 - discount_pct), 2)

        if our_price and our_price > MAX_OUR_PRICE:
            continue

        products.append({
            "name":         name,
            "product_id":   product_id,
            "our_price":    our_price,
            "msrp":         msrp,
            "discount_pct": discount_pct,
            "rating":       None,     # filled in by Stamped API pass
            "reviews":      None,
            "url":          link,
        })

    return products


def get_total_pages(html):
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".amount, p.amount, .toolbar-number")
    if el:
        nums = re.findall(r"[\d,]+", el.get_text(" ", strip=True))
        if nums:
            return math.ceil(int(nums[-1].replace(",", "")) / ITEMS_PER_PAGE)
    return 1


def extract_stamped_creds(html):
    """Prefer credentials embedded in the live page over the constants."""
    key = re.search(r"apiKey\s*[:=]\s*['\"](pubkey-[^'\"]+)", html)
    st  = re.search(r"storeUrl\s*[:=]\s*['\"]([^'\"]+)", html)
    return (key.group(1) if key else STAMPED_API_KEY,
            st.group(1) if st else STAMPED_STORE_URL)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def extract_brands(html):
    """Canonical brand names from the sidebar filter, longest first."""
    soup = BeautifulSoup(html, "html.parser")
    brands = {li.get("data-text", "").strip()
              for li in soup.select('li[class*="amshopby-attr-brands"]')}
    brands.discard("")
    return sorted(brands, key=lambda b: -len(_norm(b)))


def match_brand(name, brands):
    """Longest brand whose words lead the product name (normalized)."""
    n = _norm(name)
    for b in brands:
        nb = _norm(b)
        if n == nb or n.startswith(nb + " "):
            return b
    return None

# ── Stamped.io ratings ────────────────────────────────────────────────────────
def fetch_ratings(product_ids, api_key, store_url):
    """Return {product_id(str): (rating, review_count)} via Stamped's API."""
    ratings = {}
    ids = [pid for pid in product_ids if pid]
    for i in range(0, len(ids), STAMPED_BATCH):
        batch = ids[i:i + STAMPED_BATCH]
        payload = json.dumps({
            "productIds": [{"productId": pid, "productSKU": "",
                            "productTitle": ""} for pid in batch],
            "apiKey": api_key,
            "storeUrl": store_url,
        }).encode()
        req = urllib.request.Request(
            "https://stamped.io/api/widget/badges", data=payload,
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                for row in json.load(resp):
                    ratings[str(row.get("productId"))] = (
                        float(row.get("rating") or 0),
                        int(row.get("count") or 0))
        except Exception as e:
            print(f"  Stamped API error (batch {i//STAMPED_BATCH + 1}): "
                  f"{type(e).__name__}: {e}")
    return ratings

# ── Page fetcher ──────────────────────────────────────────────────────────────
async def _fetch_once(context, url):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
        try:
            await page.wait_for_selector("li.swatch-item", timeout=15_000)
        except:
            pass
        # evaluate() instead of content(): content() takes a navigation
        # lock and can deadlock on this perpetually-loading site.
        return await page.evaluate("document.documentElement.outerHTML")
    finally:
        await page.close()


async def fetch_page(context, pg, attempts=3):
    url = f"{BASE_URL}?limit={ITEMS_PER_PAGE}&p={pg}"
    for attempt in range(1, attempts + 1):
        try:
            # Hard watchdog: no attempt may exceed 45s no matter which
            # Playwright call stalls.
            html = await asyncio.wait_for(_fetch_once(context, url),
                                          timeout=45)
            if html and "swatch-item" in html:
                return pg, html
            print(f"  Page {pg} attempt {attempt}: loaded but no products")
        except asyncio.TimeoutError:
            print(f"  Page {pg} attempt {attempt}: hard 45s watchdog hit")
        except Exception as e:
            print(f"  Page {pg} attempt {attempt} error: {type(e).__name__}: {e}")
        if attempt < attempts:
            await asyncio.sleep(3 * attempt)
    return pg, None

# ── Crawler ───────────────────────────────────────────────────────────────────
async def _block_junk(route):
    req = route.request
    if (req.resource_type in BLOCKED_RESOURCE_TYPES
            or any(b in req.url for b in BLOCKED_URL_PARTS)):
        await route.abort()
    else:
        await route.continue_()


async def crawl():
    start = time.time()
    candidates = []
    stats = {"parsed": 0, "discount_ok": 0}
    creds = (STAMPED_API_KEY, STAMPED_STORE_URL)
    brands = []

    async with async_playwright() as pw:
        launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        try:
            browser = await pw.chromium.launch(channel="chrome",
                                               headless=True, args=launch_args)
            print("Using system Google Chrome")
        except Exception:
            browser = await pw.chromium.launch(headless=True, args=launch_args)
            print("Using bundled Chromium")

        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.route("**/*", _block_junk)

        print("Loading page 1…")
        _, html1 = await fetch_page(context, 1)
        if not html1:
            print("Failed to load page 1. Aborting.")
            await browser.close()
            return [], stats, creds, brands

        creds  = extract_stamped_creds(html1)
        brands = extract_brands(html1)
        print(f"({len(brands)} brands harvested from sidebar filter)")

        total_pages = get_total_pages(html1)
        if MAX_PAGES is not None:
            total_pages = min(total_pages, MAX_PAGES)
        print(f"→ Scanning {total_pages} page(s)  "
              f"({total_pages * ITEMS_PER_PAGE} products)\n")

        def process(html):
            found = []
            for p in parse_products(html):
                stats["parsed"] += 1
                if p["discount_pct"] >= MIN_DISCOUNT:
                    stats["discount_ok"] += 1
                    found.append(p)
                    price = f"${p['our_price']:.2f}" if p["our_price"] else "N/A"
                    print(f"  • candidate: {p['name']}  "
                          f"{p['discount_pct']*100:.0f}% off  {price}")
            return found

        candidates += process(html1)

        remaining = list(range(2, total_pages + 1))
        for i in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[i:i + BATCH_SIZE]
            print(f"Pages {batch[0]}–{batch[-1]} of {total_pages}…")
            results = await asyncio.gather(
                *[fetch_page(context, pg) for pg in batch])
            for pg, html in sorted(results):
                if html:
                    candidates += process(html)

        await browser.close()

    elapsed = time.time() - start
    print(f"\nCrawl finished in {elapsed/60:.1f} min  ({elapsed:.0f}s)")
    return candidates, stats, creds, brands


# ── Run ───────────────────────────────────────────────────────────────────────
candidates, stats, creds, brands = asyncio.run(crawl())

# Tag each candidate with its brand (longest-match against sidebar list)
for c in candidates:
    c["brand"] = match_brand(c["name"], brands)

# Second pass: ratings from Stamped, ONLY for discount+price survivors
print(f"\nFetching ratings for {len(candidates)} candidate(s) "
      f"({math.ceil(len(candidates)/STAMPED_BATCH) if candidates else 0} "
      f"API call(s))…")
ratings = fetch_ratings([c["product_id"] for c in candidates], *creds)

qualifying = []
for c in candidates:
    r = ratings.get(str(c["product_id"]))
    if r:
        c["rating"], c["reviews"] = r
    if c["rating"] and c["rating"] >= MIN_RATING:
        qualifying.append(c)

qualifying.sort(key=lambda x: (-x["discount_pct"], -(x["rating"] or 0)))

print(f"Funnel: {stats['parsed']} parsed (≤${MAX_OUR_PRICE:.0f})  |  "
      f"{stats['discount_ok']} at ≥{MIN_DISCOUNT*100:.0f}% off  |  "
      f"{len([c for c in candidates if c['rating']])} with a rating  |  "
      f"{len(qualifying)} at ≥{MIN_RATING}★")

# ── Delta filtering against sent history ──────────────────────────────────────
history = load_history() if not FORCE else set()
new_deals = [c for c in qualifying if c["url"] not in history]
skipped   = len(qualifying) - len(new_deals)

print(f"\n{'═'*68}")
print(f"  Cigars ≥{MIN_DISCOUNT*100:.0f}% off, ≥{MIN_RATING}★, "
      f"≤${MAX_OUR_PRICE:.0f}   →   {len(qualifying)} found, "
      f"{len(new_deals)} new"
      + (f" ({skipped} already sent)" if skipped else ""))
print(f"{'═'*68}\n")

for c in new_deals:
    price = f"${c['our_price']:.2f}" if c["our_price"] else "N/A"
    msrp  = f"${c['msrp']:.2f}"      if c["msrp"]      else "N/A"
    brand = f"[{c['brand']}]  " if c.get("brand") else ""
    print(f"{brand}{c['name']}")
    print(f"  Our price {price}  (MSRP {msrp})  "
          f"({c['discount_pct']*100:.0f}% off)  "
          f"★{c['rating']:.1f} ({c['reviews']} reviews)")
    print(f"  {c['url']}\n")

# ── Save CSV (new deals only) ─────────────────────────────────────────────────
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f, fieldnames=["brand", "name", "our_price", "msrp", "discount_pct",
                       "rating", "reviews", "url"],
        extrasaction="ignore")
    writer.writeheader()
    writer.writerows(new_deals)
print(f"✓ CSV saved to {OUTPUT_CSV}\n")

# ── Email (only if there are new deals) ───────────────────────────────────────
if not new_deals:
    print("No new deals since last run — no email sent.")
elif not SENDER_PASSWORD:
    print("ERROR: GMAIL_PASSWORD environment variable not set.")
    print("  History NOT updated — these deals will be retried next run.")
else:
    print("Sending email…")

    body_lines = [
        f"Found {len(new_deals)} NEW cigar deal(s) — "
        f"≥{MIN_DISCOUNT*100:.0f}% off, ≥{MIN_RATING}★, "
        f"≤${MAX_OUR_PRICE:.0f}"
        + (f"  ({skipped} previously sent, skipped)" if skipped else "")
        + "\n",
        "=" * 60,
    ]

    # Group by brand; alphabetical brands, unbranded last; alpha within
    by_brand = {}
    for c in new_deals:
        by_brand.setdefault(c.get("brand") or "Other", []).append(c)
    brand_order = sorted((b for b in by_brand if b != "Other"),
                         key=str.lower)
    if "Other" in by_brand:
        brand_order.append("Other")

    for b in brand_order:
        items = sorted(by_brand[b], key=lambda c: c["name"].lower())
        body_lines.append(f"\n──── {b} ({len(items)}) ────")
        for c in items:
            price = f"${c['our_price']:.2f}" if c["our_price"] else "N/A"
            msrp  = f"${c['msrp']:.2f}"      if c["msrp"]      else "N/A"
            body_lines += [
                f"\n{c['name']}",
                f"  Our price {price}  (MSRP {msrp})  "
                f"({c['discount_pct']*100:.0f}% off)  "
                f"★{c['rating']:.1f} ({c['reviews']} reviews)",
                f"  {c['url']}",
            ]
    body_lines += ["\n" + "="*60, "\nFull results attached as CSV."]

    msg = MIMEMultipart()
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = TO_EMAIL
    msg["Subject"] = f"🍵 Cigar Deals: {len(new_deals)} new"
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    with open(OUTPUT_CSV, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        'attachment; filename="cigar_deals.csv"')
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, TO_EMAIL, msg.as_string())
        print(f"✓ Email sent to {TO_EMAIL}")

        if not FORCE:
            history.update(c["url"] for c in new_deals)
            save_history(history)
            print(f"✓ {len(new_deals)} URL(s) added to "
                  f"{os.path.basename(HISTORY_FILE)}")
        else:
            print("Force mode — history not updated.")
    except Exception as e:
        print(f"✗ Email failed: {e}")
        print("  History NOT updated — these deals will be retried next run.")
