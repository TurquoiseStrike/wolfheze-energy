"""Wholesale market indicators from the public EnergyZero API (no key needed).

Variable tariffs follow wholesale power and gas prices with a lag of 1-3 months, so these are the leading
signal for where tariffs are heading. Prices are EPEX day-ahead (power, EUR/kWh) and the daily gas price
(EUR/m3), both excl. VAT. Hourly data is fetched in one request per commodity and averaged here.

  python scripts/market.py --out market.json     # fetch and print a summary
"""

import argparse
import json
import statistics
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

API = "https://api.energyzero.nl/v1/energyprices"
HISTORY_START = date(2024, 1, 1)
USAGE = {"power": 1, "gas": 2}


def fetch_hourly(kind, start, end, timeout=60):
    """[(utc datetime, price)] for [start, end)."""
    url = (f"{API}?fromDate={start.isoformat()}T00:00:00.000Z&tillDate={end.isoformat()}T00:00:00.000Z"
           f"&interval=4&usageType={USAGE[kind]}&inclBtw=false")
    req = urllib.request.Request(url, headers={"User-Agent": "wolfheze-energy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return [(datetime.fromisoformat(p["readingDate"].replace("Z", "+00:00")), float(p["price"]))
            for p in payload.get("Prices", [])]


def local_day(moment):
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/Amsterdam")).date()
    except Exception:
        return (moment + timedelta(hours=1)).date()


def daily_averages(hourly):
    buckets = defaultdict(list)
    for moment, price in hourly:
        buckets[local_day(moment)].append(price)
    return {d: sum(v) / len(v) for d, v in sorted(buckets.items())}


def monthly_averages(daily):
    buckets = defaultdict(list)
    for d, v in daily.items():
        buckets[d.strftime("%Y-%m")].append(v)
    return {m: sum(v) / len(v) for m, v in sorted(buckets.items())}


def window_avg(daily, end, days):
    vals = [v for d, v in daily.items() if end - timedelta(days=days) < d <= end]
    return sum(vals) / len(vals) if vals else None


def summarize(daily, today):
    """Trend and volatility figures for one commodity."""
    if not daily:
        return None
    last = max(daily)
    monthly = monthly_averages(daily)
    complete = [m for m in monthly if m < today.strftime("%Y-%m")]
    changes = []
    for prev, cur in zip(complete, complete[1:]):
        if monthly[prev] > 0:
            changes.append(monthly[cur] / monthly[prev] - 1)
    recent = changes[-24:]
    a30, p30 = window_avg(daily, last, 30), window_avg(daily, last - timedelta(days=30), 30)
    a90, p90 = window_avg(daily, last, 90), window_avg(daily, last - timedelta(days=90), 90)
    year_ago = window_avg(daily, last - timedelta(days=365), 30)
    pct = lambda a, b: round(a / b - 1, 4) if a is not None and b else None  # noqa: E731
    return {
        "last_day": last.isoformat(),
        "avg_30d": round(a30, 5) if a30 is not None else None,
        "change_30d": pct(a30, p30),
        "avg_90d": round(a90, 5) if a90 is not None else None,
        "change_90d": pct(a90, p90),
        "change_vs_year_ago": pct(a30, year_ago),
        "monthly_volatility": round(statistics.pstdev(recent), 4) if len(recent) >= 3 else None,
        "monthly": [{"month": m, "avg": round(v, 5)} for m, v in monthly.items()],
    }


def load(today, start=HISTORY_START):
    """Fetch both commodities; returns None if the API can't be reached (the dashboard then skips the section)."""
    out = {"source": "EnergyZero (EPEX day-ahead power, daily gas price), excl. VAT", "fetched_on": today.isoformat()}
    try:
        for kind in USAGE:
            daily = daily_averages(fetch_hourly(kind, start, today + timedelta(days=1)))
            out[kind] = summarize(daily, today)
            out[kind]["daily_last_120"] = [{"date": d.isoformat(), "avg": round(v, 5)}
                                           for d, v in daily.items() if d > today - timedelta(days=120)]
    except Exception as e:  # network or format problem: degrade gracefully
        print(f"NOTE: market data unavailable ({e.__class__.__name__}: {e})", file=sys.stderr)
        return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date()
    data = load(today)
    if data is None:
        return 1
    for kind in USAGE:
        s = data[kind]
        print(f"{kind}: 30d avg {s['avg_30d']} ({s['change_30d']:+.1%} vs previous 30d), "
              f"90d {s['change_90d']:+.1%}, vs year ago {s['change_vs_year_ago']:+.1%}, vol {s['monthly_volatility']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
