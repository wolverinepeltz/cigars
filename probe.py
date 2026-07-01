"""
Probe v6: stop inferring, start looking. Dumps one full product tile's
HTML, every context where 'stamped' appears in the document, any
custom elements with 'stamped'/'rating'/'review' in the tag name,
and any product-id markers we could use for a direct API fallback.
"""

import re
import time
from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"
BLOCK = ("comodo.com", "trustlogo")

with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--no-sandbox",
                                           "--disable-blink-features=AutomationControlled"])
        print("Using system Google Chrome")
    except Exception:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        print("Using bundled Chromium")

    context = browser.new_context()
    context.route("**/*", lambda route: route.abort()
                  if any(b in route.request.url for b in BLOCK)
                  else route.continue_())
    page = context.new_page()
    page.goto(URL, wait_until="load", timeout=45_000)
    time.sleep(5)
    print("✓ page loaded\n")

    # Custom elements / any tag or attribute mentioning stamped|review|rating
    hits = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('li.item.swatch-item *')) {
            const tag = el.tagName.toLowerCase();
            const attrs = [...el.attributes].map(a => a.name + '=' + a.value).join(' ');
            if (/stamped|review|rating/i.test(tag + ' ' + attrs))
                out.push((tag + ' ' + attrs).slice(0, 200));
        }
        return out.slice(0, 12);
    }""")
    print(f"── tile elements mentioning stamped/review/rating ({len(hits)}) ──")
    for h in hits:
        print(" ", h)
    if not hits:
        print("  (none)")

    html = page.evaluate("document.documentElement.outerHTML")
    browser.close()

# Every 'stamped' occurrence in the raw HTML, with context
print(f"\n── 'stamped' occurrences in document: "
      f"{len(re.findall('stamped', html, re.I))} ──")
shown = 0
for m in re.finditer("stamped", html, re.I):
    snippet = html[max(0, m.start()-60):m.start()+120].replace("\n", " ")
    print(" ", re.sub(r"\s+", " ", snippet))
    shown += 1
    if shown >= 8:
        break

# One full product tile, verbatim
tile = re.search(r'<li class="item[^"]*swatch-item.*?</li>', html, re.DOTALL)
print("\n── first product tile (first 2500 chars) ──")
print(tile.group(0)[:2500] if tile else "(tile regex found nothing)")

# Product-id markers for a potential direct-API fallback
ids = set(re.findall(r'(?:data-product-id|product-price-|product_id[=:"\s]+)(\d+)', html))
print(f"\n── product-id markers found: {len(ids)} (sample: {sorted(ids)[:5]}) ──")
