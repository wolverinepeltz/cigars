"""
Probe v5: find Stamped's trigger. Blocks broken 3rd parties, hides the
automation flag, then in order: (1) inspect raw placeholder markup and
whether StampedFn exists, (2) wait for the full 'load' event,
(3) if still nothing, force StampedFn.init() manually.
"""

import re
import time
from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"

BLOCK = ("comodo.com", "trustlogo", "klaviyo", "googletagmanager",
         "google-analytics", "doubleclick", "facebook")

JS_DIAG = """
() => ({
    stampedfn:   typeof window.StampedFn,
    stamped_els: document.querySelectorAll('[class*="stamped"]').length,
    badges:      document.querySelectorAll('.stamped-badge[data-rating]').length,
    nonzero:     [...document.querySelectorAll('.stamped-badge[data-rating]')]
                   .filter(b => parseFloat(b.getAttribute('data-rating')) > 0).length,
    sample_el:   document.querySelector('li.item.swatch-item [class*="stamped"]')
                   ?.outerHTML.slice(0, 250) ?? '(no stamped-classed element in tiles)',
    readyState:  document.readyState,
})
"""

def report(page, label):
    d = page.evaluate(JS_DIAG)
    print(f"\n[{label}] readyState={d['readyState']}  StampedFn={d['stampedfn']}  "
          f"stamped els={d['stamped_els']}  badges w/rating={d['badges']}  nonzero={d['nonzero']}")
    print(f"  sample: {d['sample_el']}")
    return d

api_calls = []

with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        print("Using system Google Chrome")
    except Exception:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        print("Using bundled Chromium")

    context = browser.new_context()
    context.route("**/*", lambda route: route.abort()
                  if any(b in route.request.url for b in BLOCK)
                  else route.continue_())
    page = context.new_page()
    page.on("response", lambda r: api_calls.append(f"{r.status} {r.url[:160]}")
            if "stamped.io/api" in r.url else None)

    page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
    print("✓ DOMContentLoaded")
    report(page, "after DCL")

    # Theory 1: full load event
    try:
        page.wait_for_load_state("load", timeout=30_000)
        print("\n✓ full 'load' event fired")
    except Exception:
        print("\n✗ full 'load' event did not fire within 30s")
    time.sleep(5)
    d = report(page, "after load+5s")

    # Theory 2: force init manually
    if d["nonzero"] == 0:
        html = page.evaluate("document.documentElement.outerHTML")
        key = re.search(r"apiKey\s*[:=]\s*['\"]([^'\"]+)", html)
        st  = re.search(r"storeUrl\s*[:=]\s*['\"]([^'\"]+)", html)
        key = key.group(1) if key else "pubkey-gj6eXSCyUdiY2z6sIcRU22I8b7BP0N"
        st  = st.group(1) if st else "www.cigarplace.biz"
        print(f"\nForcing StampedFn.init(apiKey={key[:20]}..., storeUrl={st})")
        try:
            page.evaluate(
                "([k, s]) => window.StampedFn && StampedFn.init({apiKey: k, storeUrl: s})",
                [key, st])
            deadline = time.time() + 20
            while time.time() < deadline:
                d = page.evaluate(JS_DIAG)
                if d["nonzero"] > 0:
                    break
                time.sleep(2)
            report(page, "after forced init")
        except Exception as e:
            print(f"  init threw: {e}")

    print(f"\nStamped API calls seen: {len(api_calls)}")
    for c in api_calls[:5]:
        print(" ", c)

    print("\nSample ratings:")
    for p in page.evaluate("""() =>
        [...document.querySelectorAll('li.item.swatch-item')].slice(0, 8).map(li => ({
            name:   li.querySelector('h2.product-name a')?.textContent.trim(),
            rating: li.querySelector('.stamped-badge')?.getAttribute('data-rating') ?? null,
        }))"""):
        print(f"  ★{p['rating']}  {p['name']}")

    browser.close()
