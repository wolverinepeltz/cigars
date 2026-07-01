"""
Cigarplace Deals Scraper
------------------------
Crawls https://www.cigarplace.biz/cigars.html and emails every cigar
that is >= 60% off MSRP AND rated >= 4.5 stars AND our price <= $150.

Only NEW deals (URLs not previously emailed) are sent — sent URLs are
persisted to cigarplace_sent_history.json alongside the script, same
pattern as the smoking-hub scraper. Distinct filename so both scripts
can live in the same directory without clobbering each other's state.

Credentials: reads the Gmail App Password from the GMAIL_PASSWORD
environment variable (set in GitHub Secrets, same as the other script).

Dependencies (installed by the GitHub Actions workflow):
    pip install playwright beautifulsoup4
    python -m playwright install --with-deps chromium
"""

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
import os

SENDER_EMAIL    = "peltz.chris@gmail.com"
SENDER_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")   # set in GitHub Secrets
TO_EMAIL        = "peltz.chris@gmail.com"

MAX_PAGES       = None    # None = all pages, or e.g. 3
MIN_DISCOUNT    = 0.60    # 60% off
MIN_RATING      = 4.5
MAX_OUR_PRICE   = 150.00
BATCH_SIZE      = 8       # parallel pages at once
GOTO_TIMEOUT_MS = 30_000  # per-attempt navigation timeout (fail fast if blocked)
FORCE           = False   # True = ignore history, email everything found
# ══════════════════════════════════════════════════════════════════════════════

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio, csv, json, math, re, smtplib, time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ── State files (live alongside the script, same pattern as smoking-hub) ──────
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()   # Colab cell — no __file__, use working dir

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
        # Name & link
        name_el = item.select_one("h2.product-name a")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        link = "https://www.cigarplace.biz" + name_el.get("href", "")

        # Rating
        badge = item.select_one(".stamped-badge")
        rating = None
        if badge:
            try:
                r = float(badge.get("data-rating", 0))
                rating = r if r > 0 else None
            except (ValueError, TypeError):
                pass

        # Prices
        price_box = item.select_one(".price-box")
        if not price_box:
            continue

        regular_span = price_box.select_one(".regular-price")
        msrp_span    = price_box.select_one(".msrp-price")
        savings_el   = item.select_one(".savings")   # NOTE: on item, not price_box

        msrp = parse_price(msrp_span.get_text()) if msrp_span else None

        # Discount % from site's pre-calculated value
        discount_pct = 0.0
        if savings_el:
            m = re.search(r"(\d+)%", savings_el.get_text())
            if m:
                discount_pct = int(m.group(1)) / 100

        # Always compute our_price from MSRP + discount (most reliable)
        our_price = None
        if msrp and discount_pct > 0:
            our_price = round(msrp * (1 - discount_pct), 2)

        # Skip if over max price
        if our_price and our_price > MAX_OUR_PRICE:
            continue

        products.append({
            "name":         name,
            "our_price":    our_price,
            "msrp":         msrp,
            "discount_pct": discount_pct,
            "rating":       rating,
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


# ── Page fetcher ──────────────────────────────────────────────────────────────
async def fetch_page(context, pg, attempts=3):
    url = f"{BASE_URL}?limit={ITEMS_PER_PAGE}&p={pg}"
    for attempt in range(1, attempts + 1):
        page = await context.new_page()
        try:
            # This site never fires DOMContentLoaded (a hung third-party
            # resource), so wait only for the initial response ("commit"),
            # then poll for the product grid to confirm real content.
            await page.goto(url, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
            try:
                await page.wait_for_selector("li.swatch-item", timeout=15_000)
                # Ratings are injected by the Stamped.io JS widget after the
                # grid renders — give the badges a moment to hydrate too.
                await page.wait_for_selector(
                    ".stamped-badge[data-rating]", timeout=10_000)
            except:
                pass
            html = await page.content()
            if "swatch-item" in html:
                return pg, html
            print(f"  Page {pg} attempt {attempt}: loaded but no products "
                  f"(likely bot challenge page)")
        except Exception as e:
            print(f"  Page {pg} attempt {attempt} error: {type(e).__name__}: {e}")
        finally:
            await page.close()
        if attempt < attempts:
            await asyncio.sleep(3 * attempt)   # backoff before retrying
    return pg, None


# ── Crawler ───────────────────────────────────────────────────────────────────
async def crawl():
    start = time.time()
    qualifying = []
    stats = {"parsed": 0, "rated": 0, "discount_ok": 0}

    async with async_playwright() as pw:
        launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-dev-shm-usage",
                       "--disable-blink-features=AutomationControlled"]
        try:
            # Real Google Chrome (preinstalled on GitHub runners) is far less
            # likely to be flagged by bot detection than bundled Chromium.
            browser = await pw.chromium.launch(channel="chrome", headless=True,
                                               args=launch_args)
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

        print("Loading page 1…")
        _, html1 = await fetch_page(context, 1)
        if not html1:
            print("Failed to load page 1. Aborting.")
            return []

        total_pages = get_total_pages(html1)
        if MAX_PAGES is not None:
            total_pages = min(total_pages, MAX_PAGES)
        print(f"→ Scanning {total_pages} page(s)  ({total_pages * ITEMS_PER_PAGE} products)\n")

        # Save page 1 HTML for debugging (uploaded as a workflow artifact)
        with open(os.path.join(BASE_DIR, "debug_page1.html"), "w",
                  encoding="utf-8") as f:
            f.write(html1)

        def process(html):
            found = []
            for p in parse_products(html):
                stats["parsed"] += 1
                if p["rating"] is not None:
                    stats["rated"] += 1
                if p["discount_pct"] >= MIN_DISCOUNT:
                    stats["discount_ok"] += 1
                if (p["discount_pct"] >= MIN_DISCOUNT
                        and p["rating"] is not None
                        and p["rating"] >= MIN_RATING):
                    found.append(p)
                    price = f"${p['our_price']:.2f}" if p["our_price"] else "N/A"
                    print(f"  ✓ {p['name']}  {p['discount_pct']*100:.0f}% off  "
                          f"Our price {price}  ★{p['rating']:.1f}")
            return found

        qualifying += process(html1)

        remaining = list(range(2, total_pages + 1))
        for i in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[i:i + BATCH_SIZE]
            print(f"Pages {batch[0]}–{batch[-1]} of {total_pages}…")
            results = await asyncio.gather(*[fetch_page(context, pg) for pg in batch])
            for pg, html in sorted(results):
                if html:
                    qualifying += process(html)

        await browser.close()

    elapsed = time.time() - start
    print(f"\nFinished in {elapsed/60:.1f} min  ({elapsed:.0f}s)")
    print(f"Funnel: {stats['parsed']} parsed (≤${MAX_OUR_PRICE:.0f})  |  "
          f"{stats['rated']} with a rating  |  "
          f"{stats['discount_ok']} at ≥{MIN_DISCOUNT*100:.0f}% off  |  "
          f"{len(qualifying)} passed all filters")
    if stats["parsed"] > 0 and stats["rated"] == 0:
        print("WARNING: no products had ratings — the rating widget likely "
              "didn't load. Results are unreliable this run.")
    return qualifying


# ── Run ───────────────────────────────────────────────────────────────────────
qualifying = asyncio.run(crawl())
qualifying.sort(key=lambda x: (-x["discount_pct"], -(x["rating"] or 0)))

# ── Delta filtering against sent history ──────────────────────────────────────
history = load_history() if not FORCE else set()
new_deals = [c for c in qualifying if c["url"] not in history]
skipped   = len(qualifying) - len(new_deals)

print(f"\n{'═'*68}")
print(f"  Cigars ≥{MIN_DISCOUNT*100:.0f}% off, ≥{MIN_RATING}★, ≤${MAX_OUR_PRICE:.0f}"
      f"   →   {len(qualifying)} found, {len(new_deals)} new"
      + (f" ({skipped} already sent)" if skipped else ""))
print(f"{'═'*68}\n")

for c in new_deals:
    price = f"${c['our_price']:.2f}" if c["our_price"] else "N/A"
    msrp  = f"${c['msrp']:.2f}"      if c["msrp"]      else "N/A"
    disc  = f"{c['discount_pct']*100:.0f}%"
    rat   = f"★{c['rating']:.1f}"    if c["rating"]     else "N/A"
    print(f"{c['name']}")
    print(f"  Our price {price}  (MSRP {msrp})  ({disc} off)  {rat}")
    print(f"  {c['url']}\n")

# ── Save CSV (new deals only) ─────────────────────────────────────────────────
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f, fieldnames=["name","our_price","msrp","discount_pct","rating","url"]
    )
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
        f"≥{MIN_DISCOUNT*100:.0f}% off, ≥{MIN_RATING}★, ≤${MAX_OUR_PRICE:.0f}"
        + (f"  ({skipped} previously sent, skipped)" if skipped else "") + "\n",
        "=" * 60,
    ]
    for c in new_deals:
        price = f"${c['our_price']:.2f}" if c["our_price"] else "N/A"
        msrp  = f"${c['msrp']:.2f}"      if c["msrp"]      else "N/A"
        body_lines += [
            f"\n{c['name']}",
            f"  Our price {price}  (MSRP {msrp})  ({c['discount_pct']*100:.0f}% off)  ★{c['rating']:.1f}",
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
        part.add_header("Content-Disposition", 'attachment; filename="cigar_deals.csv"')
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, TO_EMAIL, msg.as_string())
        print(f"✓ Email sent to {TO_EMAIL}")

        # Only record URLs after a successful send, mirroring the other script
        if not FORCE:
            history.update(c["url"] for c in new_deals)
            save_history(history)
            print(f"✓ {len(new_deals)} URL(s) added to {os.path.basename(HISTORY_FILE)}")
        else:
            print("Force mode — history not updated.")
    except Exception as e:
        print(f"✗ Email failed: {e}")
        print("  Check SENDER_EMAIL and SENDER_PASSWORD at the top of the script.")
        print("  History NOT updated — these deals will be retried next run.")
