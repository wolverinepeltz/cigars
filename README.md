# cigars

Daily cigar deal alerts. Each store is scanned by a GitHub Actions workflow
that emails new deals and saves what it sent in `<store>_sent_history.json`,
so the same deal is only emailed again if the price drops further.

## Shared components (`dealalerts/`)

| File | What it does |
|---|---|
| `core.py` | Fetching with retries, deal rules, sent history, email, command-line options |
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
