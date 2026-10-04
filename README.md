# Wolfheze Energy

Every day a Claude cloud routine collects the variable electricity tariffs of ~15 Dutch suppliers. It ranks them by
annual cost for an all-electric house (3,500 kWh/year) and tracks the average of the daily best deal over time.
It also tracks feed-in terms (for future solar panels), Liander grid costs, Vitens water, and internet offers.

- **Dashboard:** GitHub Pages, served from `docs/`
- **Agent instructions:** [ROUTINE.md](ROUTINE.md)
- **Settings** (usage, supplier list): [config.json](config.json)
- **Math:** [scripts/update.py](scripts/update.py), which recomputes everything from the raw CSVs in `docs/data/`
- **Tests:** `python -m unittest discover tests`

To change the yearly usage, edit `annual_kwh` in `config.json`. The next run recomputes all history with the new value.
