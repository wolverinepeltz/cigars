"""
Probe v8: does the listing tile contain a brand/manufacturer field?
Dumps one COMPLETE tile plus any elements anywhere on the page whose
class/attributes/tag mention brand or manufacturer.
"""

import re
import time
from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"
BLOCK = ("comodo.com", "trustlogo")

with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--no-sandbox"])
        print("Using system Google Chrome")
    except Exception:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        print("Using bundled Chromium")

    context = browser.new_context()
    context.route("**/*", lambda route: route.abort()
                  if any(b in route.request.url for b in BLOCK)
                  else route.continue_())
    page = context.new_page()
    page.goto(URL, wait_until="commit", timeout=30_000)
    page.wait_for_selector("li.swatch-item", timeout=15_000)
    time.sleep(2)

    hits = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('*')) {
            const tag = el.tagName.toLowerCase();
            const attrs = [...el.attributes].map(a => a.name + '="' + a.value + '"').join(' ');
            if (/brand|manufacturer/i.test(tag + ' ' + attrs))
                out.push(('<' + tag + ' ' + attrs + '> text: '
                          + (el.textContent || '').trim().slice(0, 60)).slice(0, 250));
        }
        return out.slice(0, 15);
    }""")
    print(f"\n── elements mentioning brand/manufacturer ({len(hits)}) ──")
    for h in hits:
        print(" ", h)
    if not hits:
        print("  (none anywhere on the page)")

    tile = page.evaluate(
        "document.querySelector('li.item.swatch-item')?.outerHTML ?? ''")
    # strip the noisy image block to keep the dump readable
    tile = re.sub(r'<img[^>]*>', '<img …>', tile)
    print(f"\n── first tile, complete ({len(tile)} chars) ──")
    print(tile)

    browser.close()
