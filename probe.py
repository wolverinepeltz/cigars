"""
Probe v7: no browser. Query Stamped.io's public widget API directly
for ratings, using product IDs harvested from page 1 (27885 etc.)
and the site's public key. Tries the known endpoint variants and
prints raw responses so we can see the exact schema.
"""

import json
import urllib.parse
import urllib.request

API_KEY   = "pubkey-gj6eXSCyUdiY2z6sIcRU22I8b7BP0N"
STORE_URL = "www.cigarplace.biz"
PRODUCT_IDS = [27885, 11943, 12638, 13460, 13461]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Content-Type": "application/json"}

def try_request(label, url, payload=None):
    print(f"\n── {label} ──")
    print(f"   {url[:150]}")
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(1500).decode("utf-8", errors="ignore")
        print(f"   status: {resp.status}")
        print(f"   body:   {body}")
    except Exception as e:
        print(f"   failed: {type(e).__name__}: {e}")

# Variant 1: POST /api/widget/badges (bulk badge data)
try_request(
    "POST stamped.io/api/widget/badges",
    "https://stamped.io/api/widget/badges",
    {"productIds": [{"productId": pid, "productSKU": "", "productTitle": ""}
                    for pid in PRODUCT_IDS],
     "apiKey": API_KEY, "storeUrl": STORE_URL},
)

# Variant 2: same endpoint with params in the query string
qs = urllib.parse.urlencode({"apiKey": API_KEY, "storeUrl": STORE_URL})
try_request(
    "POST /api/widget/badges (creds in query string)",
    f"https://stamped.io/api/widget/badges?{qs}",
    {"productIds": [{"productId": pid} for pid in PRODUCT_IDS]},
)

# Variant 3: GET /api/widget/stats for a single product
qs = urllib.parse.urlencode({"productId": PRODUCT_IDS[0], "apiKey": API_KEY,
                             "storeUrl": STORE_URL})
try_request(
    "GET /api/widget/stats (single product)",
    f"https://stamped.io/api/widget/stats?{qs}",
)
