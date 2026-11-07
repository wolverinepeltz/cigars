"""
Last-resort fetching through a real Chrome browser (Playwright).

Some sites (Cigora) sit behind Cloudflare's JavaScript challenge, the
"Just a moment..." page. Nothing short of a real browser running that
page's JavaScript gets past it, so fetch() falls back to this when plain
requests and the Chrome-fingerprint retry are both blocked.

One browser is started per run, on first use, and reused for every page
after that, so the clearance cookie Cloudflare hands out is kept. It is
closed automatically when the run ends.

Odds are better with a visible (non-headless) browser. On Windows and Mac
it opens a normal window; on Linux it needs a screen, which GitHub's
servers get from xvfb-run. Without one it runs hidden (headless).

Needs:  pip install playwright   (uses Google Chrome if it's installed;
otherwise Playwright's own Chromium: python -m playwright install chromium)
"""

import atexit
import os
import sys
import time

CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                    "please wait", "verifying you are human", "security verification",
                    "security check", "one more step")
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
        from requests.structures import CaseInsensitiveDict
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        import json
        return json.loads(self.text)


def looks_like_challenge(title, html):
    t = (title or "").lower()
    if any(x in t for x in CHALLENGE_TITLES):
        return True
    # Cloudflare's check script only appears on check pages, even ones a
    # site has restyled with its own title and logo (Hiland's: 1.3 MB).
    if "_cf_chl_opt" in (html or ""):
        return True
    # Challenge pages are small; a real product listing that merely embeds
    # a Cloudflare widget somewhere is much bigger.
    html = html or ""
    return len(html) < 30000 and any(m in html.lower() for m in CHALLENGE_MARKERS)


def wants_headless():
    """
    Hidden (headless) Chrome only when there's no screen to draw on: Linux
    without a display (e.g. GitHub's servers not using xvfb-run), or when
    DEALALERTS_HEADLESS=1 is set. Windows and Mac always have a screen,
    and a visible browser passes bot checks more often.
    """
    forced = os.environ.get("DEALALERTS_HEADLESS")
    if forced is not None:
        return forced.strip().lower() in ("1", "true", "yes")
    return sys.platform.startswith("linux") and not os.environ.get("DISPLAY")


class RealBrowser:
    def __init__(self):
        from playwright.sync_api import sync_playwright  # ImportError if missing
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"]
        # Try the best combination first; fall back if a browser or a
        # visible window isn't available here.
        attempts = [("chrome", False), (None, False), ("chrome", True), (None, True)]
        if wants_headless():
            attempts = [a for a in attempts if a[1]]
        self._browser, last_error = None, None
        for channel, headless in attempts:
            try:
                opts = {"channel": channel} if channel else {}
                self._browser = self._pw.chromium.launch(headless=headless, args=args, **opts)
                break
            except Exception as e:
                last_error = e
        if self._browser is None:
            raise last_error
        which = "Google Chrome" if channel else "Chromium"
        print(f"  started {which} ({'headless' if headless else 'visible window'})")
        self._context = self._browser.new_context(
            locale="en-US", timezone_id="America/Denver",
            viewport={"width": 1366, "height": 768})
        self._context.add_init_script(STEALTH_JS)
        self._page = self._context.new_page()
        atexit.register(self.close)

    def get(self, url, params=None, timeout=None, raw=False, **_):
        """
        Load url and return a BrowserResponse. With raw=True the body is the
        server's exact response (e.g. JSON from an API) rather than the page
        as Chrome renders it, and the response headers are included.
        """
        if params:
            from urllib.parse import urlencode
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        resp = self._page.goto(url, wait_until="domcontentloaded",
                               timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else 0
        title, html = self._page.title(), self._page.content()
        flagged = bool(resp) and (resp.headers or {}).get("cf-mitigated") == "challenge"
        if flagged or looks_like_challenge(title, html):
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
            if raw:
                # The page reloaded itself after the check; load the URL again
                # (now with the clearance cookie) to get its exact response.
                resp = self._page.goto(url, wait_until="domcontentloaded",
                                       timeout=NAV_TIMEOUT_MS)
                if resp is not None and looks_like_challenge(self._page.title(),
                                                             self._page.content()):
                    return BrowserResponse(403, self._page.content(), url)
        if raw and resp is not None:
            try:
                body = resp.text()
            except Exception:
                body = self._page.content()
            return BrowserResponse(resp.status, body, url, dict(resp.headers))
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


class BrowserWorker:
    """
    A RealBrowser on its own thread, for multi-threaded scripts (Hiland's
    crawls with several threads). Playwright's browser can only be driven
    from the thread that started it, so every get() is handed to that one
    thread and the caller waits for the answer; pages load one at a time.
    The browser starts on the first get(); if it can't, get() raises.
    """

    def __init__(self):
        from concurrent.futures import ThreadPoolExecutor
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        self._browser = None
        atexit.register(self.close)

    def _get(self, url, params, raw):
        if self._browser is None:
            self._browser = RealBrowser()
        return self._browser.get(url, params=params, raw=raw)

    def get(self, url, params=None, raw=True, timeout=None, **_):
        return self._exec.submit(self._get, url, params, raw).result()

    def close(self):
        if self._browser is not None:
            try:
                self._exec.submit(self._browser.close).result(timeout=30)
            except Exception:
                pass
            self._browser = None
        self._exec.shutdown(wait=False)
