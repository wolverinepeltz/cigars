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

| Store | Script | Platform |
|---|---|---|
| Cigar King | `cigarking_deal_alert.py` | BigCommerce |
| Cigars N Cigars | `cigarsncigars_deal_alert.py` | BigCommerce |
| Corona Cigar | `coronacigar_deal_alert.py` | BigCommerce |
| GT Cigars | `gtcigars_deal_alert.py` | Shopify |

The older scrapers (Hiland's, Cigarplace, Smoke Inn, Perfect Cigar Blend,
Cigora, Smoking Hub) are still standalone scripts.

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

A store on a new platform needs a new module next to `bigcommerce.py` with a
`scan(session, args)` method; everything else is shared.

## Options every store script takes

    --min-discount 33 --max-price 125     main rule
    --alt-min-discount 25 --alt-min-price 100 --alt-max-price 125
                                          optional second rule
    --dry-run                             find deals, don't email or save
    --max-pages 1                         quick test
