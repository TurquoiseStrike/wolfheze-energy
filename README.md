# Wolfheze Energy

Tracks the best variable electricity deal for an all-electric house in Wolfheze (3,500 kWh/year), plus internet,
water and solar feed-in terms. Dashboard: https://turquoisestrike.github.io/wolfheze-energy/

## How it works
1. **Research (Claude routine, daily ~05:00 UTC)** follows [ROUTINE.md](ROUTINE.md). It starts from the checklist
   `python scripts/update.py --todo` and records raw data only: tariff versions in `data/tariffs.csv`, checks in
   `data/checks.csv`, and offers in `data/internet.csv`.
2. **GitHub Actions** ([.github/workflows/site.yml](.github/workflows/site.yml)) runs on every push and daily at 06:30 UTC.
   It runs the tests and the privacy check, builds the site with `update.py --out`, and publishes it to Pages. The
   daily run also emails the digest ([scripts/email_digest.py](scripts/email_digest.py)).

## Data model
A variable tariff changes on known dates, so each row in `data/tariffs.csv` is a tariff **version** (supplier +
`valid_from`). The price on any day is the newest version valid that day. That gives complete daily series, the
"cheapest over 30/90/180/365 days" comparison, and averages without gaps.

## Commands
```bash
python scripts/update.py --todo            # today's research checklist
python scripts/update.py --check           # validate data/ and print today's summary
python scripts/update.py --out _site       # build the site
python scripts/email_digest.py --dashboard _site/dashboard.json --site-url URL --dry-run
python -m unittest discover tests
```

## Secrets (repo settings → Secrets and variables → Actions)
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO` (comma-separated) and `PRIVATE_PATTERNS` (regex of private data that
must never appear in the repo).

To change the yearly usage, edit `annual_kwh` in `config.json`. Everything is recalculated on the next build.
