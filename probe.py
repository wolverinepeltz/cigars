"""
Probe v2: fetch page 1, then diagnose WHY rating badges don't hydrate.
Counts badge elements before/after scrolling, waits longer, and reports
whether the Stamped.io script is even present on the page.
"""

import time
from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"

JS_BADGE_STATS = """
() => ({
    badges:      document.querySelectorAll('.stamped-badge').length,
    with_rating: document.querySelectorAll('.stamped-badge[data-rating]').length,
    nonzero:     [...document.querySelectorAll('.stamped-badge[data-rating]')]
                   .filter(b => parseFloat(b.getAttribute('data-rating')) > 0).length,
    stamped_script: !!document.querySelector('script[src*="stamped"]')
                    || [...document.scripts].some(s => (s.src||'').includes('stamped')
                                                    || (s.text||'').includes('Stamped')),
})
"""

JS_SAMPLE = """
() => [...document.querySelectorAll('li.item.swatch-item')].slice(0, 5).map(li => ({
    name:   li.querySelector('h2.product-name a')?.textContent.trim() ?? null,
    rating: li.querySelector('.stamped-badge')?.getAttribute('data-rating') ?? null,
    badge_html: li.querySelector('.stamped-badge')?.outerHTML.slice(0, 120) ?? '(no .stamped-badge element)',
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
    page.wait_for_selector("li.swatch-item", timeout=15_000)
    print("✓ product grid appeared")

    print("\nBEFORE scroll:", page.evaluate(JS_BADGE_STATS))

    # Scroll through the whole page to trigger any lazy-loading widgets
    height = page.evaluate("document.body.scrollHeight")
    for y in range(0, height + 800, 800):
        page.evaluate(f"window.scrollTo(0, {y})")
        time.sleep(0.25)
    print("✓ scrolled to bottom")

    # Give the widget up to 30s after scrolling
    deadline = time.time() + 30
    stats = None
    while time.time() < deadline:
        stats = page.evaluate(JS_BADGE_STATS)
        if stats["nonzero"] > 0:
            break
        time.sleep(2)

    print("AFTER scroll+wait:", stats)
    print("\nSample products:")
    for p in page.evaluate(JS_SAMPLE):
        print(f"  {p['name']}")
        print(f"    rating: {p['rating']}")
        print(f"    badge:  {p['badge_html']}")

    browser.close()
