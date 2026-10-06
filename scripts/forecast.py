"""Month-by-month cost forecasts, supplier pass-through, and forecast scoring.

Model (deliberately simple and explainable):
- A variable tariff = energy tax + VAT + the supplier's *supply price*. The supply price tracks the wholesale
  power price with a lag of L months: supply(M) = margin + W(M - L).
- L is estimated per supplier from its own tariff history (pass-through); without enough history we use
  DEFAULT_LAG. The margin is anchored on the supplier's latest known (or announced) version.
- Future wholesale W is taken flat at the latest 30-day average (base), and scaled by +-k for the high/low
  scenarios, with k = monthly volatility x sqrt(months ahead), capped.
- Costs are weighted by the household's monthly consumption profile (a heat pump uses most in winter).
Every logged forecast is scored later against the price that actually applied.
"""

import math
from datetime import date

DEFAULT_LAG = 2
MAX_LAG = 3
MAX_SCENARIO = 0.6


# ---------- month helpers ----------

def month_key(d):
    return d.strftime("%Y-%m")


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def months_between(a, b):
    """Whole months from month of a to month of b."""
    return (b.year - a.year) * 12 + b.month - a.month


# ---------- prices ----------

def energy_tax_ex(cfg, year):
    """Energy tax (1st bracket, excl. VAT) for a year; latest known year if not published yet."""
    table = cfg["taxes"]["energy_tax_eur_kwh_ex_vat"]
    known = sorted(int(y) for y in table)
    use = max([y for y in known if y <= year], default=known[0])
    return table[str(use)], use == year


def supply_ex(kwh_incl, cfg, year):
    """Supplier's own price excl. VAT and energy tax, from a tariff incl. both."""
    tax, _ = energy_tax_ex(cfg, year)
    return kwh_incl / (1 + cfg["taxes"]["vat"]) - tax


def price_incl(supply, cfg, year):
    tax, _ = energy_tax_ex(cfg, year)
    return (max(supply, 0.0) + tax) * (1 + cfg["taxes"]["vat"])


def wholesale_monthly(market):
    return {m["month"]: m["avg"] for m in market["power"]["monthly"]} if market else {}


class Wholesale:
    """W(month): actual monthly average where known, else a scenario level."""

    def __init__(self, market, today):
        self.actual = wholesale_monthly(market)
        self.today = today
        s = market["power"] if market else {}
        self.level = s.get("avg_30d")
        self.vol = s.get("monthly_volatility") or 0.15
        self.current_month = month_key(today)

    def ok(self):
        return bool(self.actual) and self.level is not None

    def at(self, month_start, scenario=0.0):
        key = month_key(month_start)
        if key < self.current_month and key in self.actual:
            return self.actual[key]
        h = max(1, months_between(self.today, month_start))
        k = min(MAX_SCENARIO, self.vol * math.sqrt(h))
        return self.level * (1 + scenario * k)


# ---------- pass-through ----------

def pass_through(versions, cfg, wholesale):
    """Estimate (lag, margin) from a supplier's tariff history; None without >= 4 monthly points."""
    if not wholesale.ok():
        return None
    first = min(v["valid_from"] for v in versions)
    months = []
    m = date(first.year, first.month, 1)
    while month_key(m) < wholesale.current_month:
        current = None
        for v in versions:
            if v["valid_from"] <= m:
                current = v
        if current is not None:
            months.append((m, supply_ex(current["kwh_price"], cfg, m.year)))
        m = add_months(m, 1)
    best = None
    for lag in range(MAX_LAG + 1):
        points = [s - wholesale.actual[month_key(add_months(mm, -lag))] for mm, s in months
                  if month_key(add_months(mm, -lag)) in wholesale.actual]
        if len(points) < 4:
            continue
        mean = sum(points) / len(points)
        spread = math.sqrt(sum((p - mean) ** 2 for p in points) / len(points))
        if best is None or spread < best["spread"]:
            best = {"lag_months": lag, "margin": round(mean, 5), "spread": round(spread, 5), "points": len(points)}
    return best


# ---------- forecasts ----------

def consumption(cfg, month_start):
    hh = cfg["household"]
    return hh["annual_kwh"] * hh["monthly_profile"][month_start.month - 1]


def last_change_month(anchor_month, m, change_months):
    """The month the price was last allowed to change, at or before m (never before the anchor)."""
    if not change_months:
        return m
    cur = m
    while cur > anchor_month:
        if cur.month in change_months:
            return cur
        cur = add_months(cur, -1)
    return anchor_month


def variable_path(versions, cfg, wholesale, start, months, scenario=0.0, lag=None, change_months=None):
    """Monthly incl.-tax kWh prices for a variable contract from `start` for `months` months.

    `change_months` (e.g. {1, 7}) limits when the supplier may change the price, as modelcontracts do;
    between those months the price stays at its last level. None means it can change every month."""
    lag = DEFAULT_LAG if lag is None else lag
    out = []
    for i in range(months):
        m = add_months(start, i)
        anchor = None
        for v in versions:
            if v["valid_from"] <= m:
                anchor = v
        if anchor is None:
            return None
        a_month = date(anchor["valid_from"].year, anchor["valid_from"].month, 1)
        a_supply = supply_ex(anchor["kwh_price"], cfg, a_month.year)
        if not wholesale.ok():
            out.append(price_incl(a_supply, cfg, m.year))
            continue
        margin = a_supply - wholesale.at(add_months(a_month, -lag))
        step = last_change_month(a_month, m, change_months)
        out.append(price_incl(margin + wholesale.at(add_months(step, -lag), scenario), cfg, m.year))
    return out


def dynamic_path(markup_incl, cfg, wholesale, start, months, scenario=0.0):
    """Monthly average kWh price on a dynamic contract (spot + markup + tax), flat daily load shape."""
    if not wholesale.ok():
        return None
    out = []
    for i in range(months):
        m = add_months(start, i)
        out.append(price_incl(wholesale.at(m, scenario), cfg, m.year) + markup_incl)
    return out


def path_cost(prices, cfg, start, fixed_month):
    return round(sum(p * consumption(cfg, add_months(start, i)) for i, p in enumerate(prices)) + 12 * fixed_month, 2)


def first_year(versions, cfg, wholesale, start, fixed_month, lag=None, change_months=None):
    """Base/low/high first-year cost and the base monthly prices for a variable contract."""
    base = variable_path(versions, cfg, wholesale, start, 12, 0.0, lag, change_months)
    if base is None:
        return None
    low = variable_path(versions, cfg, wholesale, start, 12, -1.0, lag, change_months)
    high = variable_path(versions, cfg, wholesale, start, 12, 1.0, lag, change_months)
    return {"base": path_cost(base, cfg, start, fixed_month), "low": path_cost(low, cfg, start, fixed_month),
            "high": path_cost(high, cfg, start, fixed_month),
            "monthly_kwh_price": [round(p, 5) for p in base]}


# ---------- logging and scoring ----------

FORECAST_FIELDS = ["made_on", "supplier", "target_month", "kwh_base", "kwh_low", "kwh_high"]


def change_months_for(cfg, supplier, product_kind):
    """Months a supplier may change its price, from its contract terms in config; None = any month."""
    per = cfg.get("price_change_months", {})
    if supplier in per:
        return set(per[supplier])
    return None  # no restriction known: the price can follow the market every month


def forecast_rows(chains, cfg, wholesale, made_on):
    """Next 1-3 month kWh price forecasts for each supplier, for the log.

    `chains` maps supplier -> (the history of its current product, its product kind)."""
    rows = []
    for supplier, (versions, kind) in chains.items():
        pt = pass_through(versions, cfg, wholesale)
        lag = pt["lag_months"] if pt else None
        cm = change_months_for(cfg, supplier, kind)
        start = add_months(date(made_on.year, made_on.month, 1), 1)
        base = variable_path(versions, cfg, wholesale, start, 3, 0.0, lag, cm)
        low = variable_path(versions, cfg, wholesale, start, 3, -1.0, lag, cm)
        high = variable_path(versions, cfg, wholesale, start, 3, 1.0, lag, cm)
        if base is None:
            continue
        for i in range(3):
            rows.append({"made_on": made_on.isoformat(), "supplier": supplier,
                         "target_month": month_key(add_months(start, i)),
                         "kwh_base": round(base[i], 5), "kwh_low": round(min(low[i], high[i]), 5),
                         "kwh_high": round(max(low[i], high[i]), 5)})
    return rows


def score(rows, tariffs, today):
    """Accuracy of logged forecasts whose target month has started, by horizon (months ahead)."""
    by_h = {}
    for r in rows:
        target = date.fromisoformat(r["target_month"] + "-01")
        if target > today or r["supplier"] not in tariffs:
            continue
        actual = None
        for v in tariffs[r["supplier"]]:
            if v["valid_from"] <= target:
                actual = v
        if actual is None:
            continue
        h = months_between(date.fromisoformat(r["made_on"]), target)
        a = actual["kwh_price"]
        b = by_h.setdefault(h, {"n": 0, "abs_pct": 0.0, "in_band": 0})
        b["n"] += 1
        b["abs_pct"] += abs(float(r["kwh_base"]) - a) / a
        b["in_band"] += float(r["kwh_low"]) <= a <= float(r["kwh_high"])
    return [{"horizon_months": h, "n": b["n"], "mape": round(b["abs_pct"] / b["n"], 4),
             "band_hit_rate": round(b["in_band"] / b["n"], 3)} for h, b in sorted(by_h.items())]
