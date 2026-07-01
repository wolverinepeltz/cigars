"""
Probe v4: block the broken parser-blocking third-party scripts
(comodo trustlogo et al.), which should let the page's load event
fire and the Stamped rating widget initialize. Then check badges.
"""

import time
from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"

BLOCK = ("comodo.com", "trustlogo", "klaviyo", "googletagmanager",
         "google-analytics", "doubleclick", "facebook")

JS_BADGE_STATS = """
() => ({
    badges:      document.querySelectorAll('.stamped-badge').length,
    with_rating: document.querySelectorAll('.stamped-badge[data-rating]').length,
    nonzero:     [...document.querySelectorAll('.stamped-badge[data-rating]')]
                   .filter(b => parseFloat(b.getAttribute('data-rating')) > 0).length,
})
"""

JS_SAMPLE = """
() => [...document.querySelectorAll('li.item.swatch-item')].slice(0, 8).map(li => ({
    name:   li.querySelector('h2.product-name a')?.textContent.trim() ?? null,
    rating: li.querySelector('.stamped-badge')?.getAttribute('data-rating') ?? null,
}))
"""

stamped_api = []

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
    page.on("response", lambda r: stamped_api.append(f"{r.status} {r.url[:160]}")
            if "stamped.io/api" in r.url else None)

    print(f"Loading {URL} (blocking broken 3rd-party scripts) ...")
    t0 = time.time()
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        print(f"✓ DOMContentLoaded fired after {time.time()-t0:.1f}s !!")
    except Exception as e:
        print(f"✗ DOMContentLoaded still didn't fire: {e}")
        page.wait_for_selector("li.swatch-item", timeout=15_000)

    # give Stamped time to fetch ratings
    deadline = time.time() + 25
    stats = None
    while time.time() < deadline:
        stats = page.evaluate(JS_BADGE_STATS)
        if stats["nonzero"] > 0:
            break
        time.sleep(2)

    print("Badge stats:", stats)
    print(f"Stamped API calls seen: {len(stamped_api)}")
    for s in stamped_api[:5]:
        print(" ", s)

    print("\nSample products:")
    for p in page.evaluate(JS_SAMPLE):
        print(f"  ★{p['rating']}  {p['name']}")

    browser.close()
