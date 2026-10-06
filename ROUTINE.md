# Daily routine: Wolfheze energy deal tracker

You are the research agent for a household moving to Wolfheze (gemeente Renkum, Gelderland): 2 adults, an
all-electric house, ~3,500 kWh/year, comparing **variable** electricity contracts. You collect prices from the web
and record them as raw data in `data/`. You **never** compute costs, rankings or averages. `scripts/update.py` does
that, and GitHub Actions builds and publishes the dashboard and sends the daily email after you push.

## The data model: tariff versions
A variable tariff changes on known dates (often the 1st of the month, or 1 January / 1 July). So `data/tariffs.csv`
holds one row per **tariff version**: a supplier's prices plus the date they're valid from. The price on any day is
the newest version valid that day. That means:
- **If a supplier's price hasn't changed, add nothing to tariffs.csv.** Record the check in `data/checks.csv` instead.
- **Add a tariff row only for a version that isn't recorded yet**: a new valid-from date, a correction, or a version
  you can now complete (e.g. you found the fixed charge, or the official sheet for a comparison-site price).
- **A correction** is a new row with the same `supplier` + `valid_from` and a later `found_at`. The latest one wins.
  Never edit or delete existing rows.

## Always start with the checklist
```bash
python3 scripts/update.py --todo
```
It prints today's work:
- `NO TARIFF` / `NO FIXED CHARGE` — find the current tariff or its fixed charge.
- `UPGRADE SOURCE` — the price is only from a comparison site; find the supplier's official tariff sheet.
- `WEEKLY RETRY (blocked site)` — Mondays only, for suppliers in `sources.json` → `blocked`: one honest try via the
  document routes, then move on. The owner fills these in via *Help wanted* (`data/manual.csv`).
- `RESOLVE ASSUMPTION` — confirm what the row assumed (e.g. whether VAT is included) and add a corrected row.
- `CHECK FOR NEW VERSION` — not checked for a week, or it's the start of a month.
- `OFFERS CHECK DUE` — fixed 1/3-year and dynamic offers (weekly).
- `INTERNET CHECK DUE`, `MONTHLY CHECK DUE`, `TAX TABLE`
- `BACKFILL` — low priority, only at the end (see below).

**Work through every item except `BACKFILL` first.** You may only finish when `--todo` prints "nothing left", or each
remaining non-backfill item has had a real attempt: at least two different sources, **including the browser** (see
below). List every remaining item
with what you tried in your final message. Ending after a few minutes with items left is a failed run. A run on a
day that already has data continues the list. It never means "today is done".

## Honesty rules
- Every row needs a `source_url` to the page or PDF where you saw the price. No URL means no row.
- Never estimate, guess, or carry a value forward yourself. `update.py` handles missing fixed charges by itself.
- `valid_from` is the date the source says the tariff applies from (*geldig vanaf*, "per 1 oktober"). If the source
  doesn't say, use today's date and write "valid-from not published" in `notes`.
- `found_at` is the real moment you read it: use the output of `date -Iseconds`. Never type a made-up time.
- `source_type`: `official` = the supplier's own website, tariff sheet or press release; `comparison` = anything else.
- Record prices exactly as published. Don't round.
- **If you had to assume anything** (VAT included? energy tax included? valid for new customers?), write that
  assumption in the `assumption` column, not just in `notes`. Such rows are ranked, but not recommended until the
  assumption is resolved (the checklist will ask for it).
- Privacy: never write the household's street, postcode or house number anywhere in the repo. Before committing,
  run the privacy check from your prompt (see "Publish").

## Tools
- **WebSearch** with `allowed_domains: ["<supplier domain>"]` is the fastest way to find a tariff sheet (PDF).
- **curl** for plain pages and PDFs: `curl -sSL -m 40 -A "Mozilla/5.0" <url>`. For PDFs, `pip install pypdf` and
  extract the text with `pypdf.PdfReader`.
- **The browser** handles pages that need JavaScript or a postcode form. Most big suppliers (Vattenfall, Eneco, Essent,
  Greenchoice, ...) only show the fixed charge after you enter a postcode. Set it up once per run, then use it:
  ```bash
  bash scripts/setup_browser.sh
  python3 scripts/browse.py <url> --grep "vaste leveringskosten|per maand|kWh"
  python3 scripts/browse.py <url> --links "\.pdf|tarieven"          # find tariff sheet links
  python3 scripts/browse.py <url> --step "fill:input[name*=postcode i]=<postcode>" --step "fill:input[name*=huisnummer i]=<number>" \
      --step "click:text=Bekijk" --step "wait:3" --grep "leveringskosten|kWh"
  ```
  For energy tariffs a postcode in the 6874 area is enough. For internet availability use the exact address from your
  prompt. If a selector fails, take a `--screenshot /tmp/x.png`, look at it, and adjust.
- `sources.json` lists URLs that worked before. Try those first, and update the file with what worked today.

## 1. Energy
For each energy item on the checklist, find the current **variable** (*variabel*, *modelcontract variabel*) electricity
tariff for a **new customer** in the Liander grid area. Best sources, in order:
1. The supplier's **modelcontract tariff sheet** (PDF). Every Dutch supplier must publish one.
2. The supplier's own tariff page or calculator, using the browser with a postcode.
3. A comparison site (`config.json` → `comparison_sites`, keuze.nl, selectra.nl, ...), as `source_type=comparison`.

`data/tariffs.csv` columns:

| column | meaning |
|---|---|
| `supplier` | exactly as in `config.json` → `suppliers` (`update.py` rejects other names). Only add a genuinely new supplier to `config.json` if it beats the current best |
| `product` | product name |
| `valid_from` | `YYYY-MM-DD`, see the honesty rules |
| `kwh_single` | single tariff (*enkeltarief*) €/kWh. If only normal/off-peak are listed, leave empty and use the next two |
| `kwh_normal`, `kwh_dal` | normal / off-peak €/kWh |
| `tax_basis` | `incl` = incl. energy tax and VAT (usual); `excl_eb` = incl. VAT but excl. energy tax. Only use `excl_eb` if the source says so explicitly |
| `fixed_supply_eur_month` / `_day` / `_year` | fixed supply charge (*vaste leveringskosten*) incl. VAT, **not** grid costs. Fill exactly one, in the unit the source uses. Leave all empty if unknown |
| `welcome_bonus_eur` | welcome bonus / cashback, 0 if none |
| `feedin_payment_eur_kwh` | payment per kWh fed back (*terugleververgoeding*) |
| `feedin_cost_eur_kwh` | feed-in fee per kWh (*terugleverkosten*), only if it's one flat €/kWh rate |
| `feedin_cost_desc` | short text on the feed-in fee, e.g. "staffel €X–€Y/month", "none" |
| `source_type` | `official` or `comparison` |
| `source_url` | where you read the kWh price |
| `fixed_source_url` | where you read the fixed charge, only if it's a different page |
| `found_at` | `date -Iseconds` |
| `notes` | anything odd or ambiguous |
| `assumption` | anything you had to assume (see the honesty rules); empty if nothing |
| `product_kind` | `modelcontract` or `standard` (see "Which product" below) |

For every supplier you checked whose current version was already correct, append to `data/checks.csv`:
`date,supplier,result,notes` with `result` = `unchanged`, or `not_found` if you couldn't reach any current price.
Use `new_version` when you added a tariff row.

### Which product: compare like with like
Many suppliers have **two** variable products with different prices:
- the legally standardised **modelcontract** (at some suppliers its price only changes on 1 January and 1 July), and
- their own **standard** variable product (often changes monthly, e.g. Essent's 1 Oct 2026 increase).

Record the **cheapest variable product a new customer can sign up for**, and set `product_kind` to `modelcontract` or
`standard`. If you know both prices, add both as separate rows: the cheaper one counts, and the other is useful
history. If a source doesn't say which product a price belongs to (press releases often say just "variable"), write
that in `assumption`.

### Blocked suppliers: the document routes
Some suppliers' **web pages** refuse automated browsers (see `sources.json` → `blocked`), but their **documents** are
served from separate file paths that are usually reachable with plain `curl`. Try these, in order:
1. **Search for the PDF directly**: WebSearch with the supplier's domain and terms like `tarieven modelcontract
   consument pdf`, `tarievenblad variabel`, `tb-` (Essent's sheets are named like `tb-essent-...pdf`). Look at the
   URL patterns of documents you do find, such as the business sheet
   `essent.nl/-/media/essent-zakelijk/documenten/modelcontract/tb-essent-sme-mdc-acq-2026-04.pdf`, and try the
   consumer equivalent (e.g. `/-/media/essent/documenten/...`). Energiedirect is part of the same group, with similar
   paths under `energiedirect.nl/-/media/energiedirect/documenten/`.
2. **Document mirrors**: gaslicht.com keeps copies of supplier documents under
   `gaslicht.com/userfiles/pdf/energie/<supplier>/...` (tariff sheets as well as terms). These are copies of official
   documents: use `source_type=official` and note "mirror of the supplier's PDF" in `notes`.
3. **Press releases / newsroom pages** (e.g. `essent.nl/over-essent/nieuws/...`) aren't blocked. They often state the
   new kWh price and sometimes the fixed charge.
4. **Comparison sites** as a last resort, with `source_type=comparison`.

Never try to disguise the browser or get around a block (no stealth plugins, no proxies). If nothing works, leave
the item for the owner: it's listed under *Help wanted* on the dashboard.

### Backfill (`BACKFILL` items, low priority)
The forecast learns how fast each supplier passes wholesale price moves on to customers, which needs price history.
Add earlier tariff versions (back to 2025-01) as normal rows with their real `valid_from`. Good sources: price
history tables (Mega has one), archived tariff sheets (PDF file names often contain the date), and press releases
about price changes. **Do backfill only after every other item, and stop when the run has taken ~25 minutes.**

### Fixed and dynamic offers (`OFFERS CHECK DUE`)
The household compares all three contract types, and fixed prices are also a market signal (they price in what
suppliers expect). Record in `data/offers.csv`:
- `fixed_1y` / `fixed_3y`: the supplier's standard 1-year / 3-year fixed electricity contract for new customers.
  Price columns as in tariffs.csv.
- `dynamic`: hourly-price contracts (Frank Energie, Tibber, ANWB Energie, Zonneplan, EasyEnergy, NextEnergy, and the
  big suppliers' dynamic products). Put the supplier's markup per kWh **incl. VAT** in `dynamic_markup_eur_kwh`
  (*inkoopvergoeding*/*opslag*), plus the fixed charge. Suppliers must be in `config.json` → `suppliers` or
  `dynamic_suppliers`.

Columns: `supplier, contract_type, product, valid_from, kwh_single, kwh_normal, kwh_dal, tax_basis,
fixed_supply_eur_month/_day/_year, dynamic_markup_eur_kwh, welcome_bonus_eur, source_type, source_url, found_at,
notes, assumption`. Add a row when an offer is new or its price changed. The newest row per supplier + type counts.

### Market news (`data/events.csv`)
Whenever you come across market news while researching (a supplier announcing a price change, a regulator or tax
decision, a big wholesale move reported by the press), add a line: `date, scope (supplier/market/regulation),
supplier, type (price_change/announcement/regulation/news), summary, url`. One line per event, with no duplicates.

### Tax table (`TAX TABLE`)
Once the next year's electricity energy tax is published (Belastingplan, usually from Prinsjesdag in September), add
it to `config.json` → `taxes.energy_tax_eur_kwh_ex_vat` (1st bracket, **excl.** VAT, source: Belastingdienst or
Rijksoverheid), and update the matching `data/calendar.json` entry's `impact` text.

## 2. Internet: when the checklist says `INTERNET CHECK DUE`
`config.json` → `internet` has the household's needs (≥ 100 Mbit/s is plenty) and `address_networks`: the networks
**confirmed** at the address. Currently **DELTA Fiber**, up to 8,000 Mbit/s, confirmed by the owner.
1. Find out which providers sell internet over the DELTA Fiber network (it's an open network), and record each one's
   cheapest internet-only offer ≥ 100 Mbit/s (plus the next tier if ≤ €5/month more). Those get
   `available_at_address=yes` and `network=DELTA Fiber`.
2. Also record the main alternatives on other networks (KPN, Ziggo cable, 5G home internet). Use the browser with the
   address from your prompt to check them; `yes`/`no` only when a checker said so for this exact address, otherwise
   `unknown`.

`data/internet.csv` columns: `date, provider, product, technology (fiber/cable/dsl/5g), network, download_mbps,
upload_mbps, price_eur_month (regular price after promotions), promo_price_eur_month, promo_months, one_off_eur
(activation/installation, 0 if none), contract_months, available_at_address (yes/no/unknown), promo_desc, source_url`.
`update.py` computes the first-year cost.

## 3. Grid costs, taxes and water: when the checklist says `MONTHLY CHECK DUE`
Update `data/fixed_costs.json` (Liander grid costs per year incl. VAT for a ≤ 3x25A connection, the energy-tax refund
per year, energy tax per kWh incl. VAT, `checked_at`, `sources`) and `data/water.json` (Vitens `fixed_eur_year` and
`eur_per_m3` incl. 9% VAT, `tap_water_tax_eur_m3_excl_vat` from the Belastingdienst, `checked_at`, the source URLs).
Keep the existing keys. Copy values from official pages only. In January, make sure they're the new year's figures.

## 4. Publish
Note the run's start time at the very beginning (`date -Iseconds`) and the checklist item count from the first
`--todo`. Then, at the end:
```bash
python3 scripts/update.py --check          # must print no VALIDATION ERROR; fix the raw rows if it does
python3 scripts/update.py --todo           # anything left you haven't really tried? go back to it
python3 scripts/update.py --log-forecast   # logs today's 1-3 month price forecasts (scored later)
# append one line to data/runs.csv:
#   date,started_at,finished_at,todo_before,todo_after,new_versions,notes
python3 scripts/privacy_check.py --patterns "<the patterns from your prompt>"   # must print "clean"
git add data sources.json config.json
git commit -m "research: <date>: <what changed, e.g. 2 new versions, 3 fixed charges, internet>"
git push origin HEAD:main
```
If the push is rejected, `git pull --rebase origin main` and push again. Only commit `data/`, `sources.json` and
`config.json` (new supplier or tax table). Never commit build output.

## 5. Final message and notifications
5–10 lines: what changed (new versions, fixed charges, sources upgraded, offers, events), today's best deal and the
first-year forecast from `--check`, each checklist item still open with what you tried, and anything notable (e.g.
announced price changes). **Always end the final message, and any push notification, with the dashboard link:
https://turquoisestrike.github.io/wolfheze-energy/**
