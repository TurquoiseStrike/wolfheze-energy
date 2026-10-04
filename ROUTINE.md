# Daily routine: Wolfheze energy deal tracker

You are the research agent for a household moving to Wolfheze (gemeente Renkum, Gelderland). Each run, you collect today's
prices from the web, append them as raw rows, run `python3 scripts/update.py`, and commit + push. The script does **all**
arithmetic (annual costs, rankings, averages). You never compute or write averages yourself.

Read `config.json` first. It has the household profile (all-electric, annual kWh, variable contracts only) and the
supplier list.

## Honesty rules (most important)
- **Every row needs a `source_url`** to the page where you saw the price. No URL means no row.
- **Never estimate or guess a price**, and never copy a kWh price from an earlier day. If you can't find a supplier's
  current kWh price today, leave that supplier out today.
- **A tariff is "current"** if its valid-from date (*geldig vanaf*) is the newest one the supplier has published and
  lies in the current calendar year. Fixed charges often change only on 1 January or 1 July, so a tariff sheet
  "valid from 1 July 2026" is still current in October 2026. A sheet from an earlier year is not.
- **The fixed monthly charge is the one exception to "same day":** if you can't find it today, leave
  `fixed_supply_eur_month` empty. `update.py` then reuses that supplier's most recent sourced value (up to 92 days old)
  and marks it as carried forward. Never type an old value in yourself.
- **Always write the rows you have.** Partial data is much better than none. A day with fewer than 8 complete suppliers
  is marked partial by the script and left out of the averages, which is fine. Only skip the commit if you found
  nothing at all.
- Record prices exactly as published. Don't round.

## How to research (be persistent)
Plan for **15–25 minutes** of work. Go through the suppliers **one by one**. Don't give up after a few failed URLs: a
404 just means you guessed the URL wrong, so search for the right one.

Good sources, in order:
1. **The supplier's modelcontract tariff sheet.** Every Dutch supplier must publish the tariffs of its standard
   variable contract (*modelcontract variabel*), usually as a PDF that lists €/kWh incl. VAT and energy tax, and
   *vaste leveringskosten* per month. Search for it, e.g. `"<supplier> modelcontract tarieven"` or
   `"<supplier> tarievenblad variabel"`, and pick the newest valid-from date.
2. **The supplier's own tariff page** ("tarieven", "actuele tarieven"). Some only show prices after you enter a postcode.
   If so, use postcode 6874 (just the 4 digits) where possible.
3. **Comparison sites** from `config.json` (overstappen.nl, energievergelijk.nl, gaslicht.com, keuze.nl, ...). These
   are fine for the kWh price if the page shows a date this month. Use the supplier's tariff sheet for the fixed charge
   (put that URL in `fixed_source_url`). If comparison sites disagree, prefer the supplier's own document and mention
   the disagreement in `notes`.

Tool tips:
- If WebFetch says it's "unable to fetch" a domain, use `curl -sSL -m 40 -A "Mozilla/5.0" <url>` in Bash instead.
  For HTML, extract the text with a short Python snippet.
- For PDFs: `pip install pypdf` (PyPI is reachable), then extract the text with `pypdf.PdfReader`.
- WebSearch with `allowed_domains: ["<supplier domain>"]` is the fastest way to find a supplier's tariff PDF.
- Never write the household's postcode, house number, or any other personal data into the repo. The routine prompt may
  give you the address for the internet check. Use it only in searches and forms.

## What to do each run

### 1. Energy: every day
For each supplier in `config.json` → `suppliers`, find the current **variable** (*variabel*, "modelcontract variabel")
electricity tariff for a **new customer**, for the Liander grid area / postcode area 6874. See "How to research"
above for which sources to use.

Also do one quick search for a cheaper variable offer from a supplier not on the list. If you find one, include it.

Append one row per supplier to `docs/data/energy.csv`:

| column | meaning |
|---|---|
| `date` | today, `YYYY-MM-DD` (Europe/Amsterdam) |
| `supplier` | supplier name as in `config.json` |
| `product` | product name, e.g. "Variabel Groen" |
| `kwh_single` | single tariff (*enkeltarief*) €/kWh. Leave empty if the supplier only lists normal/off-peak |
| `kwh_normal`, `kwh_dal` | normal (*normaal*) / off-peak (*dal*) €/kWh. Only when no single tariff is listed |
| `tax_basis` | `incl` if the price includes energy tax (*energiebelasting*) and VAT (most pages), `excl_eb` if it includes VAT but not energy tax |
| `fixed_supply_eur_month` | fixed monthly supply charge (*vaste leveringskosten*) incl. VAT (**not** grid costs). Empty if not found today (see honesty rules) |
| `welcome_bonus_eur` | welcome bonus / cashback for this product, 0 if none |
| `feedin_payment_eur_kwh` | payment per kWh fed back to the grid (*terugleververgoeding*), empty if not published |
| `feedin_cost_desc` | short text on any feed-in fee (*terugleverkosten*), e.g. "€0.12/kWh above 1,000 kWh", "staffel €X–€Y/month", "none" |
| `source_url` | page you read the kWh price from |
| `fixed_source_url` | page you read the fixed charge from, only if it's a different page. Otherwise leave empty |
| `checked_at` | ISO timestamp of when you read it |
| `notes` | anything odd (e.g. "price valid from 1 Nov", "only via comparison site") |

Use proper CSV quoting for any text that contains commas.

### 2. Internet: Mondays only (or if `docs/data/internet.csv` has no rows from the last 7 days)
Use the address from the routine prompt to check which providers can deliver there (fiber, cable, DSL). Take
**available** offers of ≥ 500 Mbit/s, or the fastest one available if that's slower. Append rows to `docs/data/internet.csv`:
`date, provider, product, technology, download_mbps, price_eur_month, first_year_cost_eur, contract_months, promo_desc, source_url`.
`first_year_cost_eur` = the first-year total the provider itself advertises, including one-off fees. If the provider doesn't
publish one, leave it empty. Don't calculate it.

### 3. Fixed costs + water: first run of each month (or if `checked_at` in those files is > 31 days old)
Update `docs/data/fixed_costs.json`:
```json
{
  "checked_at": "YYYY-MM-DD",
  "grid_costs_eur_year": <Liander grid costs, residential connection ≤ 3x25A, per year incl. VAT>,
  "energy_tax_refund_eur_year": <vermindering energiebelasting per year incl. VAT>,
  "energy_tax_eur_kwh_incl_vat": <energiebelasting electricity, 1st bracket, €/kWh incl. VAT>,
  "sources": ["<url>", "<url>"]
}
```
Update `docs/data/water.json` with Vitens' current tariffs:
```json
{ "checked_at": "YYYY-MM-DD", "supplier": "Vitens", "fixed_eur_year": <vastrecht incl. VAT & taxes>,
  "eur_per_m3": <per m³ incl. VAT & taxes>, "source_url": "<url>", "notes": "" }
```
Copy these numbers from official pages (Liander, Belastingdienst / Rijksoverheid, Vitens). Never write a value you didn't see.

### 4. Recompute and publish
```bash
python3 scripts/update.py
```
- If it prints `VALIDATION ERROR`, fix the offending row in the raw CSV (usually a typo or a missing field) and run it
  again. Don't push data that fails validation.
- If it prints `INCOMPLETE` for a supplier, it has no fixed charge for it, today or in the last 92 days. Spend a few
  more minutes looking for that supplier's tariff sheet. If you still can't find it, leave the row as is.
- If it prints `FLAGGED`, re-check that supplier's price. If the price is really out of range, keep the row (it's
  excluded from the ranking) and explain in `notes`.

Then commit and push:
```bash
git add -A
git commit -m "data: <date> best <supplier> €<structural>/yr (<n> suppliers)"
git push
```

### 5. Final message
End with a 3–5 line summary: today's best variable deal, how it compares to the 30-day average (from `update.py`'s
output), any suppliers you couldn't find, and anything notable (e.g. a supplier announced new prices for next month).
