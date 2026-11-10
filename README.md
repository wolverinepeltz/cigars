# cigars

Daily cigar deal alerts. Each store is scanned by a GitHub Actions workflow
that emails deals and saves what it sent in `<store>_sent_history.json`.

## When a deal is emailed

For the stores on the shared code, a qualifying deal is emailed when it is:

| Tag in the email | Meaning |
|---|---|
| `NEW` | never emailed before |
| `↓ $8.68` | cheaper than the last email said, by that much |
| `↑ from 30%` | same price, but at least 1 point more off (MSRP/was went up) |
| `back` | it stopped qualifying (sold out, price rose, left the deals page) and now qualifies again |

Discounts shrinking or prices rising are never emailed (Hiland's is the
exception: it reports any discount change, tagged `↓ from X%` when it fell).

## The email

Every store sends the same HTML email (with a plain-text copy), built in
`dealalerts/emailfmt.py`:

- Subject: `Cigar King: 4 deals (2 new, 1 back, 1 price drop, 30-50% off)`
- Discount bands (80%+, 70-79%, ... 30-39%), then "On clearance"
- Within a band: brands A to Z, then each cigar line A to Z with its % range,
  then its vitolas, cheapest first. A cigar sits in the band of its best
  vitola, so all its sizes stay together.
- Each vitola is two lines, and the row is the link:
  `[tag] size, pack count, rating` and
  `price, was/MSRP price, % off, price per cigar`
- Prices over $125 are red. Where a store gives only "save X%", the list
  price is worked out and shown as "about $X list".

Brand, cigar line and vitola are read from the product name
(`parse_name()`); a store that knows the brand passes it as `brand`.
Details a store doesn't have (rating, pack count) are simply left out.

Each history entry looks like `{"price": 89.95, "discount": 40.0, "active": true}`.
If a run reads less than half as many items as the last one (a page failed
or the site blocked part of the run), nothing is marked inactive, so a bad
run can't cause a wave of false "back" emails. Older history files load as
they are and are upgraded on the next save.

## Blacklist

`deal_blacklist.txt` lists deals never to email, from any store: one word
or phrase per line (a brand, a cigar line, a kind of product like
`sampler`, or a product link). A deal is skipped when its name, brand or
size contains the entry as whole words; capitals and accents don't
matter. Each run's log shows what it skipped. (`blacklist.json` is the
separate domain list for the disabled Smoking Hub script.)

## Shared components (`dealalerts/`)

| File | What it does |
|---|---|
| `core.py` | Fetching with retries, deal rules, sent history, command-line options |
| `emailfmt.py` | The email every store sends (layout above) |
| `blacklist.py` | Reads `deal_blacklist.txt` and skips matching deals |
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
| Cigar Circus | `cigarcircus_deal_alert.py` | Odoo | in the script |

All of these use `core.py` for fetching, rules, history and options.
Stores on a platform with no shared module keep their page reading in their
own script and plug into `core.run()` with a small store class.

Cigar Circus shows no "was" or MSRP price, so anything in its clearance
categories at or under `--max-price` is emailed (marked `on clearance`),
along with price drops. Items elsewhere in its catalog are only emailed
if they show a struck-through price that meets the discount rules.

Still standalone, because they work differently (they send the same
email through `emailfmt.py`):

- `hilands_crawl.py` (Hiland's): settings in `config.yml`, pack-size filter,
  CSV reports in `data/` (not emailed), emails on any discount change.
- `cigarplace_scraper.py` (Cigarplace): drives a real browser (Playwright)
  and can check star ratings.
- `scraper.py` (Smoking Hub): reads a deals blog with coupon codes, not a
  store's prices. **Disabled**: its workflow has no schedule (manual runs
  only) and it keeps its own email.

## When a site blocks GitHub

`fetch()` escalates on HTTP 403: a plain request, then Chrome's connection
fingerprint (`curl_cffi`), then a real Chrome browser (`playwright`). The
first that works is used for the rest of the run. The log's 403 line names
the blocker (e.g. `server=cloudflare cf-mitigated=challenge`). On GitHub's
servers a workflow only gets the real-browser step if it installs
`playwright` and runs the script under `xvfb-run`.

Cigora, Perfect Cigar Blend and Hiland's block GitHub's servers outright
(Cloudflare challenges even a real browser there), so they run on the home
runner.

## Home runner

Cigora, Perfect Cigar Blend and Hiland's run on a Windows PC at home, through a
GitHub self-hosted runner labelled `home-cigars`. Their workflows say
`runs-on: [self-hosted, Windows, home-cigars]`; nothing else runs there.

One-time setup on the PC:

1. Install [Git for Windows](https://git-scm.com/download/win) (default
   options) if `git --version` in PowerShell doesn't print a version.
   Google Chrome should be installed too (it usually is); without it the
   workflow downloads Playwright's Chromium instead.
2. On GitHub: repo **Settings > Actions > Runners > New self-hosted runner**,
   choose **Windows** and **x64**.
3. Open **PowerShell as Administrator** and run the **Download** commands
   from that page (they make `C:\actions-runner`).
4. Run the **Configure** command from that page (`./config.cmd --url ...
   --token ...`) and answer:
   - runner group: press Enter
   - runner name: press Enter, or type `home-pc`
   - additional labels: type `home-cigars`
   - work folder: press Enter
   - run as a service: `Y`
   - service account: press Enter (`NT AUTHORITY\NETWORK SERVICE`)
5. Back on the Runners page the runner should show **Idle**.
6. Actions tab: run **Cigora deal alert** and **Perfect Cigar Blend deal
   alert** once by hand and check the logs.

The PC must be on and awake when the runs are due (they wait otherwise).
Never add a `pull_request` trigger to a workflow that runs on this runner:
this repo is public, and that would let strangers' code run on the PC.

Troubleshooting:
- Run stuck on "Waiting for a runner": the runner service isn't running
  (Windows Services > "GitHub Actions Runner").
- "Set up Python" fails: an existing Python 3.12 install can block the
  installer; remove it in Apps & Features or change `python-version`.
- Remove the runner: `C:\actions-runner\config.cmd remove --token ...`
  (token from the runner's page on GitHub).

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

    --min-discount 30 --max-price 150     main rule
    --alt-min-discount 25 --alt-min-price 100 --alt-max-price 150
                                          optional second rule
    --dry-run                             find deals, don't email or save
    --max-pages 1                         quick test
