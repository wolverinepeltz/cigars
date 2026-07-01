"""
Probe v3: watch the network. Logs every request/response involving
stamped.io, plus console errors, while the page loads and scrolls.
Then tries calling the same Stamped API endpoint directly from Python
to see if plain HTTP succeeds where the in-page widget fails.
"""

import re
import time
import urllib.request

from playwright.sync_api import sync_playwright

URL = "https://www.cigarplace.biz/cigars.html?limit=48&p=1"

stamped_events = []
console_msgs   = []

with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--no-sandbox"])
        print("Using system Google Chrome")
    except Exception:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        print("Using bundled Chromium")

    page = browser.new_page()
    page.on("response", lambda r: stamped_events.append(
        f"RESP {r.status} {r.url[:200]}") if "stamped" in r.url else None)
    page.on("requestfailed", lambda r: stamped_events.append(
        f"FAIL {r.failure} {r.url[:200]}") if "stamped" in r.url else None)
    page.on("console", lambda m: console_msgs.append(
        f"{m.type}: {m.text[:200]}") if m.type in ("error", "warning") else None)

    print(f"Loading {URL} ...")
    page.goto(URL, wait_until="commit", timeout=30_000)
    page.wait_for_selector("li.swatch-item", timeout=15_000)
    print("✓ product grid appeared")

    height = page.evaluate("document.body.scrollHeight")
    for y in range(0, height + 800, 800):
        page.evaluate(f"window.scrollTo(0, {y})")
        time.sleep(0.25)
    print("✓ scrolled; waiting 20s for widget traffic...")
    time.sleep(20)

    html = page.evaluate("document.documentElement.outerHTML")
    browser.close()

print(f"\n── stamped.io network events ({len(stamped_events)}) ──")
for e in stamped_events:
    print(" ", e)
if not stamped_events:
    print("  (none — the widget never even attempted an API call)")

print(f"\n── console errors/warnings ({len(console_msgs)}) ──")
for m in console_msgs[:15]:
    print(" ", m)

# Extract the public Stamped credentials from the page, if present
api_key  = re.search(r"apiKey\s*[:=]\s*['\"]([^'\"]+)", html)
store    = re.search(r"storeUrl\s*[:=]\s*['\"]([^'\"]+)", html)
s_id     = re.search(r"(?:sId|storeHash)\s*[:=]\s*['\"]([^'\"]+)", html)
print("\n── credentials found in page ──")
print(f"  apiKey:   {api_key.group(1) if api_key else None}")
print(f"  storeUrl: {store.group(1) if store else None}")
print(f"  sId:      {s_id.group(1) if s_id else None}")

# If the browser attempted an API call, replay the first one from Python
api_urls = [e.split(" ", 2)[-1] for e in stamped_events
            if "api" in e and "http" in e]
if api_urls:
    test_url = api_urls[0]
    print(f"\n── replaying from Python: {test_url[:120]} ──")
    try:
        req = urllib.request.Request(test_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(300).decode("utf-8", errors="ignore")
        print(f"  status: {resp.status}")
        print(f"  body:   {body}")
    except Exception as e:
        print(f"  failed: {type(e).__name__}: {e}")
