# Wolfheze Energy

Decision support for an all-electric house in Wolfheze (3,500 kWh/year, moving in ~1 Dec 2026). It tracks
variable electricity tariffs as dated versions, compares them with fixed and dynamic contracts, follows the
wholesale market, and forecasts the first-year cost with a range. Internet, water and solar feed-in terms are
included. Dashboard: https://turquoisestrike.github.io/wolfheze-energy/

## How it works
1. **Research (Claude routine, daily ~05:00 UTC)** follows [ROUTINE.md](ROUTINE.md). It starts from
   `python scripts/update.py --todo` and records raw data only, in `data/`:
   - `tariffs.csv` — tariff versions
   - `offers.csv` — fixed and dynamic offers
   - `checks.csv`, `events.csv`, `internet.csv`, `runs.csv`
   - `forecasts.csv` — the forecast log
2. **GitHub Actions** ([site.yml](.github/workflows/site.yml)) runs on every push and daily at 06:30 UTC:
   tests → privacy check → build (fetches wholesale prices from EnergyZero) → browser smoke test → Pages. The daily
   run also emails the digest via Brevo ([email_digest.py](scripts/email_digest.py)).

## Model
- **Tariff versions:** the price on any day is the newest version valid that day (`update.py`).
- **Forecast** ([forecast.py](scripts/forecast.py)): a supplier's supply price follows wholesale power with a lag
  (measured per supplier once there's history; 2 months by default). Costs are weighted by a heat pump's monthly use.
  The range comes from wholesale volatility. Forecasts are logged daily and scored after the fact (Operations panel).
- **Market** ([market.py](scripts/market.py)): EnergyZero day-ahead power and gas, monthly averages since 2024.

## Commands
```bash
python scripts/update.py --todo                 # today's research checklist
python scripts/update.py --check                # validate data/ and print today's summary + forecast
python scripts/update.py --out _site            # build the site
python scripts/update.py --log-forecast         # append today's forecasts to data/forecasts.csv
python scripts/email_digest.py --dashboard _site/dashboard.json --site-url URL --dry-run
python -m unittest discover tests               # unit tests
python tests/smoke_site.py _site                # browser smoke test (needs playwright)
```

## Adding a price yourself
Some suppliers block automated browsers or hide prices behind a postcode form (see *Help wanted* on the dashboard).
Copy the price from their website into [data/manual.csv](data/manual.csv) using the GitHub web editor. The format
is shown on the dashboard. Manual prices count as confirmed.

## Runbook
| Symptom | What to do |
|---|---|
| Email says "update failed" | Open the linked Actions run. A `VALIDATION ERROR` names the bad row: fix it on github.com, or start the routine again. A privacy-check failure means private data reached the repo: remove it and rewrite history if it was pushed |
| Banner "agent recorded nothing today" | Check the routine at claude.ai/code/routines (paused? usage limit?). Run it manually |
| A supplier stays on *Help wanted* | Add it to `data/manual.csv` (about 2 minutes) |
| No email arrives | Check the spam folder, and the Brevo dashboard (sender verified? daily limit?). The job prints Brevo's error |
| Market section empty | The EnergyZero API was unreachable during the build. It recovers on the next build |
| New tax year (January) | The checklist's `TAX TABLE` item asks the agent to add the new rate; verify it in `config.json` |

## Secrets (repo settings → Secrets and variables → Actions)
- `BREVO_API_KEY` — Brevo API key
- `EMAIL_FROM` — the sender address verified in Brevo
- `EMAIL_TO` — comma-separated recipients
- `PRIVATE_PATTERNS` — regex of private data that must never appear in the repo
