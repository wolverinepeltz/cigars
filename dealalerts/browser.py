"""
Last-resort fetching through a real Chrome browser (Playwright).

Some sites (Cigora) sit behind Cloudflare's JavaScript challenge, the
"Just a moment..." page. Nothing short of a real browser running that
page's JavaScript gets past it, so fetch() falls back to this when plain
requests and the Chrome-fingerprint retry are both blocked.

One browser is started per run, on first use, and reused for every page
after that, so the clearance cookie Cloudflare hands out is kept. It is
closed automatically when the run ends.

Odds are better with a visible (non-headless) browser, so the workflow runs
the script under xvfb-run, a virtual screen. Without a screen it falls back
to headless mode.

Needs:  pip install playwright   (uses the Google Chrome preinstalled on
GitHub's ubuntu runners; falls back to Playwright's own Chromium)
"""

import atexit
import os
import time

CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                    "please wait", "verifying you are human")
CHALLENGE_MARKERS = ("challenge-platform", "cf-chl-", "cf_chl_opt", "turnstile")
CHALLENGE_WAIT = 30      # seconds to let a challenge clear
NAV_TIMEOUT_MS = 45_000

# Hide the most obvious signs of automation.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


class BrowserResponse:
    """Just enough of a requests.Response for fetch() and the stores."""

    def __init__(self, status_code, text, url, headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.text)


def looks_like_challenge(title, html):
    t = (title or "").lower()
    if any(x in t for x in CHALLENGE_TITLES):
        return True
    # Challenge pages are small; a real product listing that merely embeds
    # a Cloudflare widget somewhere is much bigger.
    html = html or ""
    return len(html) < 30000 and any(m in html.lower() for m in CHALLENGE_MARKERS)


class RealBrowser:
    def __init__(self):
        from playwright.sync_api import sync_playwright  # ImportError if missing
        self._pw = sync_playwright().start()
        headless = not os.environ.get("DISPLAY")
        args = ["--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"]
        try:
            self._browser = self._pw.chromium.launch(channel="chrome",
                                                     headless=headless, args=args)
            which = "Google Chrome"
        except Exception:
            self._browser = self._pw.chromium.launch(headless=headless, args=args)
            which = "Chromium"
        print(f"  started {which} ({'headless' if headless else 'on a virtual screen'})")
        self._context = self._browser.new_context(
            locale="en-US", timezone_id="America/Denver",
            viewport={"width": 1366, "height": 768})
        self._context.add_init_script(STEALTH_JS)
        self._page = self._context.new_page()
        atexit.register(self.close)

    def get(self, url, params=None, timeout=None, **_):
        if params:
            from urllib.parse import urlencode
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        resp = self._page.goto(url, wait_until="domcontentloaded",
                               timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else 0
        title, html = self._page.title(), self._page.content()
        if looks_like_challenge(title, html):
            print("  challenge page; letting the browser work through it ...")
            deadline = time.time() + CHALLENGE_WAIT
            while time.time() < deadline:
                self._page.wait_for_timeout(1000)
                try:
                    title, html = self._page.title(), self._page.content()
                except Exception:
                    continue  # mid-navigation while the challenge redirects
                if not looks_like_challenge(title, html):
                    status = 200
                    break
            else:
                return BrowserResponse(403, html, url, {"page-title": title})
        if status == 0:
            status = 200
        return BrowserResponse(status, html, url)

    def close(self):
        for closer in (getattr(self, "_context", None), getattr(self, "_browser", None)):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if getattr(self, "_pw", None):
                self._pw.stop()
                self._pw = None
        except Exception:
            pass


_SHARED = {}  # one browser per process: Playwright allows only one per thread


def real_browser(session=None):
    """The process's RealBrowser, started on first use; None if unavailable."""
    if "browser" not in _SHARED:
        try:
            _SHARED["browser"] = RealBrowser()
        except ImportError:
            print("  playwright isn't installed, so can't try a real browser "
                  "(pip install playwright).")
            return None  # not cached: nothing was started
        except Exception as e:
            print(f"  couldn't start a browser ({type(e).__name__}: {e})")
            _SHARED["browser"] = None
    return _SHARED["browser"]
