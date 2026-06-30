"""
Cigar Deals Scraper
-------------------
Polls smoking-hub.com/cigar-deals/ and emails only NEW deals
whenever the page has been updated since the last check.
Designed to run on GitHub Actions — reads credentials from
environment variables and persists state files to the repo.
"""

import re
import smtplib
import json
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

TARGET_URL = "https://smoking-hub.com/cigar-deals/"

EMAIL_CONFIG = {
    "smtp_host":  "smtp.gmail.com",
    "smtp_port":  587,
    "username":   "peltz.chris@gmail.com",
    "password":   os.environ.get("GMAIL_PASSWORD", ""),   # set in GitHub Secrets
    "from_addr":  "peltz.chris@gmail.com",
    "to_addrs":   ["peltz.chris@gmail.com"],
}

# State files live alongside the script in the repo
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
BLACKLIST_FILE = os.path.join(BASE_DIR, "blacklist.json")
HISTORY_FILE   = os.path.join(BASE_DIR, "sent_history.json")
STATE_FILE     = os.path.join(BASE_DIR, "state.json")

DEFAULT_BLACKLIST = [
    # affiliate / tracker networks
    "pntrs.com", "gopjn.com", "pjtra.com", "anrdoezrs.net",
    "pntrac.com", "dpbolvw.net", "pjatr.com", "kqzyfj.com",
    "jdoqocy.com", "tkqlhce.com", "pntra.com", "awin1.com",
    "pxf.io",
    # social / utility
    "facebook.com", "instagram.com", "reddit.com", "x.com",
    "twitter.com", "linkedin.com", "pinterest.com", "youtube.com",
    "discord.com", "t.me", "telegram.org",
    "googletagmanager.com", "policies.google.com",
    # own site infra
    "smoking-hub.com", "smokingmarketing.com",
    # user blacklist
    "cigarpage.com", "cigardealhunters.com", "cigarsinternational.com",
    "jrcigars.com", "famous-smoke.com", "thompsoncigar.com",
    "bestcigarprices.com", "cigora.com",
]

# ─────────────────────────────────────────────
# BLACKLIST
# ─────────────────────────────────────────────

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            return set(json.load(f))
    bl = set(DEFAULT_BLACKLIST)
    save_blacklist(bl)
    return bl

def save_blacklist(bl):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(sorted(bl), f, indent=2)

def add_to_blacklist(domain):
    bl = load_blacklist()
    bl.add(domain.lower().lstrip("www."))
    save_blacklist(bl)
    print(f"Added '{domain}' to blacklist.")

def remove_from_blacklist(domain):
    bl = load_blacklist()
    bl.discard(domain.lower().lstrip("www."))
    save_blacklist(bl)
    print(f"Removed '{domain}' from blacklist.")

def show_blacklist():
    print("Current blacklist:")
    for d in sorted(load_blacklist()):
        print(f"  - {d}")

def is_blacklisted(domain, blacklist):
    domain = domain.lower().lstrip("www.")
    return any(domain == b or domain.endswith("." + b) for b in blacklist)

# ─────────────────────────────────────────────
# SENT HISTORY
# ─────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    return set()

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(sorted(history), f, indent=2)

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_updated_time": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────

def fetch_page(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CigarScraper/1.0)"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def parse_updated_time(html):
    match = re.search(
        r'<meta[^>]+property=["\']og:updated_time["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:updated_time["\']',
            html, re.IGNORECASE
        )
    return match.group(1).strip() if match else None

def strip_tags(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_deals(html, blacklist, history):
    deals = []
    seen_urls = set()

    col_chunks = re.split(r'(?=<div[^>]+class="[^"]*zb-column[^"]*")', html)

    for chunk in col_chunks:
        link_match = re.search(
            r'<a[^>]+class="[^"]*zb-el-container[^"]*"[^>]+href=["\']([^"\']+)["\']',
            chunk, re.IGNORECASE
        )
        if not link_match:
            continue

        href = link_match.group(1).strip()

        if not href.startswith("http"):
            continue
        if href in seen_urls or href in history:
            continue

        # ── CHANGED: follow redirects before blacklist check ──
        try:
            req = Request(href, headers={"User-Agent": "Mozilla/5.0 (compatible; CigarScraper/1.0)"})
            with urlopen(req, timeout=10) as r:
                href = r.url  # update to final destination URL
        except Exception:
            pass  # keep original href if redirect fails
        # ─────────────────────────────────────────────────────

        parsed = urlparse(href)
        domain = parsed.netloc.lower().lstrip("www.")
        if is_blacklisted(domain, blacklist):
            continue

        seen_urls.add(href)

        # Headline (brown, #361500)
        headline = ""
        desc_block = re.search(
            r'class="zb-el-imageBox-description"[^>]*>(.*?)</div>',
            chunk, re.DOTALL
        )
        price = ""
        if desc_block:
            desc_html = desc_block.group(1)
            h_match = re.search(r'color:\s*#361500[^>]*>([^<]+)', desc_html, re.IGNORECASE)
            if h_match:
                headline = h_match.group(1).strip()
            else:
                s_match = re.search(r'<strong[^>]*>(.*?)</strong>', desc_html, re.DOTALL | re.IGNORECASE)
                if s_match:
                    headline = strip_tags(s_match.group(1))

            # Price (red, #a61a32)
            p_match = re.search(r'color:\s*#a61a32[^>]*>(.*?)(?:</strong>|</span>)', desc_html, re.IGNORECASE | re.DOTALL)
            if p_match:
                price = strip_tags(p_match.group(1))

        # Brand/description (zb-el-zionText after </a>)
        after_link = chunk[link_match.end():]
        brand_texts = []
        for zt_match in re.finditer(
            r'class="zb-el-zionText"[^>]*>.*?<p[^>]*>(.*?)</p>',
            after_link, re.DOTALL | re.IGNORECASE
        ):
            text = strip_tags(zt_match.group(1))
            if text and not text.startswith("*Props") and len(text) > 3:
                brand_texts.append(text)
        description = " | ".join(brand_texts) if brand_texts else ""

        # Coupon codes
        coupons = re.findall(
            r'<input[^>]+class="[^"]*coupon-code[^"]*"[^>]+value="([^"]+)"',
            chunk, re.IGNORECASE
        )
        seen_c = set()
        unique_coupons = []
        for c in coupons:
            if c not in seen_c:
                seen_c.add(c)
                unique_coupons.append(c)

        title = headline or price
        if not title or len(title) < 4:
            continue

        deals.append({
            "title":       title,
            "price":       price,
            "description": description,
            "coupons":     unique_coupons,
            "url":         href,
            "domain":      domain,
        })

    return deals

# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def build_email(deals, updated_time):
    today   = datetime.now().strftime("%B %d, %Y")
    subject = f"🍂 New Cigar Deals — {today}"

    by_domain = {}
    for d in deals:
        by_domain.setdefault(d["domain"], []).append(d)

    html_parts = [f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#222">
    <h2 style="color:#5a3e2b">🍂 New Cigar Deals — {today}</h2>
    <p style="color:gray;font-size:12px">Page last updated: {updated_time}<br>
    Source: <a href="{TARGET_URL}">{TARGET_URL}</a></p>
    """]

    for domain, items in sorted(by_domain.items()):
        html_parts.append(
            f'<h3 style="color:#5a3e2b;border-bottom:1px solid #ddd">'
            f'{domain} <span style="font-size:13px;color:gray">({len(items)})</span></h3>'
        )
        for item in items:
            html_parts.append('<div style="margin-bottom:18px;padding:12px;border:1px solid #eee;border-radius:6px">')
            html_parts.append(f'<div style="font-weight:bold;color:#361500">{item["title"]}</div>')
            if item["price"]:
                html_parts.append(f'<div style="color:#4CAF50;margin-top:4px">{item["price"]}</div>')
            if item["description"]:
                html_parts.append(f'<div style="color:#555;font-size:13px;margin-top:6px">{item["description"]}</div>')
            if item["coupons"]:
                for code in item["coupons"]:
                    html_parts.append(
                        f'<div style="margin-top:8px">'
                        f'<span style="background:#fff1e2;border:1px solid #c9965c;color:#603601;'
                        f'padding:4px 10px;border-radius:4px;font-family:monospace;font-size:14px">'
                        f'🏷 {code}</span></div>'
                    )
            html_parts.append(
                f'<div style="margin-top:10px">'
                f'<a href="{item["url"]}" style="background:#a61a32;color:white;padding:7px 16px;'
                f'border-radius:5px;text-decoration:none;font-size:14px">Buy From Here →</a></div>'
            )
            html_parts.append('</div>')

    html_parts.append(
        f"<hr><p style='color:gray;font-size:11px'>"
        f"{len(deals)} new deals across {len(by_domain)} domains</p>"
        f"</body></html>"
    )

    text_parts = [f"New Cigar Deals — {today}", "=" * 40, f"Page updated: {updated_time}", ""]
    for domain, items in sorted(by_domain.items()):
        text_parts.append(f"\n{'='*30}\n{domain.upper()}\n{'='*30}")
        for item in items:
            text_parts.append(f"\n{item['title']}")
            if item["price"]:
                text_parts.append(f"  {item['price']}")
            if item["description"]:
                text_parts.append(f"  Brands: {item['description']}")
            if item["coupons"]:
                text_parts.append(f"  Coupon(s): {', '.join(item['coupons'])}")
            text_parts.append(f"  {item['url']}")

    return subject, "\n".join(text_parts), "".join(html_parts)

def send_email(subject, text_body, html_body):
    cfg = EMAIL_CONFIG
    if not cfg["password"]:
        print("ERROR: GMAIL_PASSWORD environment variable not set.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["from_addr"]
    msg["To"]      = ", ".join(cfg["to_addrs"])
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["from_addr"], cfg["to_addrs"], msg.as_string())

    print(f"Email sent to: {', '.join(cfg['to_addrs'])}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run(force=False):
    print(f"Fetching {TARGET_URL} ...")
    state     = load_state()
    blacklist = load_blacklist()
    history   = load_history() if not force else set()

    html         = fetch_page(TARGET_URL)
    updated_time = parse_updated_time(html)

    print(f"Page updated_time: {updated_time}")
    print(f"Last seen time:    {state['last_updated_time']}")

    if force:
        print("Force mode — skipping update-time and history checks.")
    else:
        if updated_time and updated_time == state["last_updated_time"]:
            print("Page not updated since last check — nothing to send.")
            return

    deals = extract_deals(html, blacklist, history)
    print(f"Found {len(deals)} deals after filtering.")

    if not deals:
        print("No deals to send.")
        if not force and updated_time:
            state["last_updated_time"] = updated_time
            save_state(state)
        return

    subject, text_body, html_body = build_email(deals, updated_time)
    send_email(subject, text_body, html_body)

    if not force:
        history.update(d["url"] for d in deals)
        save_history(history)
        if updated_time:
            state["last_updated_time"] = updated_time
            save_state(state)
    else:
        print("Force mode — history and state not updated.")

# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) == 1:
        run()
    elif sys.argv[1] == "--force":
        run(force=True)
    elif sys.argv[1] == "blacklist":
        if len(sys.argv) == 2:
            show_blacklist()
        elif sys.argv[2] == "add" and len(sys.argv) == 4:
            add_to_blacklist(sys.argv[3])
        elif sys.argv[2] == "remove" and len(sys.argv) == 4:
            remove_from_blacklist(sys.argv[3])
        else:
            print("Usage: python3 scraper.py blacklist [add|remove] <domain>")
    elif sys.argv[1] == "history":
        if len(sys.argv) == 2:
            h = load_history()
            print(f"{len(h)} URLs in sent history.")
        elif sys.argv[2] == "clear":
            save_history(set())
            print("History cleared.")
    else:
        print("Usage:")
        print("  python3 scraper.py                         # run — email if page updated")
        print("  python3 scraper.py --force                 # send all deals, ignore history")
        print("  python3 scraper.py blacklist               # show blacklist")
        print("  python3 scraper.py blacklist add cigora.com")
        print("  python3 scraper.py blacklist remove cigora.com")
        print("  python3 scraper.py history                 # count of sent URLs")
        print("  python3 scraper.py history clear           # reset sent history")
