# cigars

Daily cigar deal alerts. Each store is scanned by a GitHub Actions workflow
that emails deals and saves what it sent in `<store>_sent_history.json`.

## When a deal is emailed

For the stores on the shared code, a qualifying deal is emailed when it is:

| Label | Meaning |
|---|---|
| `NEW` | never emailed before |
| `price dropped from $X` | cheaper than the last email said |
| `discount up from X%` | same price, but at least 1 point more off (MSRP/was went up) |
| `back` | it stopped qualifying (sold out, price rose, left the deals page) and now qualifies again |

Discounts shrinking or prices rising are never emailed. The subject line
counts each kind, e.g. `Cigar King: 4 deal(s) (2 new, 1 back, 1 price drop)`.

Each history entry looks like `{"price": 89.95, "discount": 40.0, "active": true}`.
If a run reads less than half as many items as the last one (a page failed
or the site blocked part of the run), nothing is marked inactive, so a bad
run can't cause a wave of false "back" emails. Older history files load as
they are and are upgraded on the next save.

## Shared components (`dealalerts/`)

| File | What it does |
|---|---|
| `core.py` | Fetching with retries, deal rules, sent history, email, command-line options |
| `browser.py` | Last-resort fetching through real Chrome, for sites behind Cloudflare's "Just a moment..." check |
| `bigcommerce.py` | Reads product tiles from BigCommerce stores and finds their deal pages |
| `shopify.py` | Reads the product feed from Shopify stores |

| Store | Script | Platform | Page reading |
|---|---|---|---|
| Cigar King | `cigarking_deal_alert.py` | BigCommerce | shared |
| Cigars N Cigars | `cigarsncigars_deal_alert.py` | BigCommerce | shared |
| Corona Cigar | `coronacigar_deal_alert.py` | BigCommerce | shared |
| GT Cigars | `gtcigars_deal_alert.py` | Shopify | shared |
| Smoke Inn | `smokeinn_deal_alert.py` | Miva | in the script |
| Perfect Cigar Blend | `pcb_deal_alert.py` | WooCommerce | in the script |
| Cigora | `cigora_deal_alert.py` | Salesforce Commerce | in the script |

All of these use `core.py` for fetching, rules, history, email and options.
Stores on a platform with no shared module keep their page reading in their
own script and plug into `core.run()` with a small store class.

Still standalone, because they work differently:

- `hilands_crawl.py` (Hiland's): settings in `config.yml`, pack-size filter,
  CSV reports in `data/`, emails on any discount change.
- `cigarplace_scraper.py` (Cigarplace): drives a real browser (Playwright)
  and can check star ratings.
- `scraper.py` (Smoking Hub): reads a deals blog with coupon codes, not a
  store's prices.

## When a site blocks GitHub

`fetch()` escalates on HTTP 403: a plain request, then Chrome's connection
fingerprint (`curl_cffi`), then a real Chrome browser (`playwright`). The
first that works is used for the rest of the run. The log's 403 line names
the blocker (e.g. `server=cloudflare cf-mitigated=challenge`). A workflow
only gets the real-browser step if it installs `playwright` and runs the
script under `xvfb-run` (see the Cigora workflow).

## Adding a store

1. Find the platform. View the page source: `bigcommerce.stencil` or
   `cdn11.bigcommerce.com` means BigCommerce; `cdn.shopify.com` or
   `/collections/.../products/` URLs mean Shopify.
2. Copy the script of a store on the same platform and change its settings:
   name, site address, history file, and deal pages (BigCommerce) or
   collections (Shopify).
3. Copy its workflow in `.github/workflows/`, change the names, and give it a
   free time slot.
4. Run it once from the Actions tab and check the log.

A store on a platform with no shared module needs a store class with a
`scan(session, args)` method that returns its items; see `pcb_deal_alert.py`
for a short example. Everything else is shared. If a second store on that
platform comes along, move the page reading into a module next to
`bigcommerce.py`.

## Options every store script takes

    --min-discount 33 --max-price 125     main rule
    --alt-min-discount 25 --alt-min-price 100 --alt-max-price 125
                                          optional second rule
    --dry-run                             find deals, don't email or save
    --max-pages 1                         quick test
