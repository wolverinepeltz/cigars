"""
Barebones diagnostic: fetch ONLY page 1 of cigarplace.biz and print
what we can extract. No email, no history, no filters, no bs4 —
data is pulled straight out of the live DOM with one evaluate() call
(deliberately avoids page.content(), which can hang on this site).

Run: pip install playwright && python probe.py
(uses system Google Chrome; falls back to bundled Chromium)
"""

from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"

JS_EXTRACT = """
() => [...document.querySelectorAll('li.item.swatch-item')].map(li => ({
    name:     li.querySelector('h2.product-name a')?.textContent.trim() ?? null,
    msrp:     li.querySelector('.price-box .msrp-price')?.textContent.trim() ?? null,
    savings:  li.querySelector('.savings')?.textContent.trim() ?? null,
    rating:   li.querySelector('.stamped-badge')?.getAttribute('data-rating') ?? null,
}))
"""

with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--no-sandbox"])
        print("Using system Google Chrome")
    except Exception:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        print("Using bundled Chromium")

    page = browser.new_page()
    print(f"Loading {URL} ...")
    page.goto(URL, wait_until="commit", timeout=30_000)

    try:
        page.wait_for_selector("li.swatch-item", timeout=15_000)
        print("✓ product grid appeared")
    except Exception:
        print("✗ product grid never appeared")

    try:
        page.wait_for_selector(".stamped-badge[data-rating]", timeout=10_000)
        print("✓ rating badges appeared")
    except Exception:
        print("✗ rating badges never appeared (10s)")

    products = page.evaluate(JS_EXTRACT)
    browser.close()

print(f"\n{len(products)} products on page 1")
rated    = sum(1 for p in products if p["rating"] not in (None, "", "0"))
saved    = sum(1 for p in products if p["savings"])
priced   = sum(1 for p in products if p["msrp"])
print(f"  with rating:  {rated}")
print(f"  with savings: {saved}")
print(f"  with msrp:    {priced}\n")

for p in products[:10]:
    print(f"name:    {p['name']}")
    print(f"msrp:    {p['msrp']}")
    print(f"savings: {p['savings']}")
    print(f"rating:  {p['rating']}")
    print("-" * 50)
