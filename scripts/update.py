"""Build the Wolfheze energy dashboard from the raw research data in data/.

The research agent only appends raw rows; every number is computed here.

  python scripts/update.py --todo        what today's research run still has to do (writes nothing)
  python scripts/update.py --check       validate data/ and print today's summary (writes nothing)
  python scripts/update.py --out _site   validate and build the static site (used by CI)

Exit code 1 means the raw data failed validation.

Data model: a variable tariff changes on known dates, so data/tariffs.csv holds one row per
tariff *version* (supplier + valid_from). The price on any day is the newest version valid
that day, which gives complete daily series without re-recording unchanged prices.
"""

import argparse
import csv
import json
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forecast  # noqa: E402
import market as market_data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://turquoisestrike.github.io/wolfheze-energy/"
REPO_URL = "https://github.com/TurquoiseStrike/wolfheze-energy"

TARIFF_FIELDS = [
    "supplier", "product", "valid_from", "kwh_single", "kwh_normal", "kwh_dal", "tax_basis",
    "fixed_supply_eur_month", "fixed_supply_eur_day", "fixed_supply_eur_year", "welcome_bonus_eur",
    "feedin_payment_eur_kwh", "feedin_cost_eur_kwh", "feedin_cost_desc",
    "source_type", "source_url", "fixed_source_url", "found_at", "notes", "assumption", "product_kind",
]
TARIFF_REQUIRED = ["supplier", "valid_from", "source_type", "source_url", "found_at"]
# Suppliers often have two variable products with different prices: the legally standardised modelcontract
# (price changes only 1 Jan / 1 Jul at some suppliers) and their own standard variable product.
PRODUCT_KINDS = ("", "modelcontract", "standard")
# official = supplier's own site/sheet; manual = owner copied it from the supplier's site; comparison = third party
SOURCE_TYPES = ("official", "manual", "comparison")

# Owner-entered prices (data/manual.csv), kept deliberately simple for editing on github.com.
MANUAL_FIELDS = ["supplier", "valid_from", "kwh_single", "kwh_normal", "kwh_dal", "fixed_supply_eur_month",
                 "source_url", "entered_on", "notes"]

OFFER_FIELDS = [
    "supplier", "contract_type", "product", "valid_from", "kwh_single", "kwh_normal", "kwh_dal", "tax_basis",
    "fixed_supply_eur_month", "fixed_supply_eur_day", "fixed_supply_eur_year", "dynamic_markup_eur_kwh",
    "welcome_bonus_eur", "source_type", "source_url", "found_at", "notes", "assumption",
]
OFFER_REQUIRED = ["supplier", "contract_type", "valid_from", "source_type", "source_url", "found_at"]
CONTRACT_TYPES = ("fixed_1y", "fixed_3y", "dynamic")
CONTRACT_LABELS = {"variable": "variable", "fixed_1y": "fixed 1-year", "fixed_3y": "fixed 3-year", "dynamic": "dynamic"}

EVENT_FIELDS = ["date", "scope", "supplier", "type", "summary", "url"]
RUN_FIELDS = ["date", "started_at", "finished_at", "todo_before", "todo_after", "new_versions", "notes"]
FORECAST_LOG_FIELDS = forecast.FORECAST_FIELDS

CHECK_FIELDS = ["date", "supplier", "result", "notes"]
CHECK_RESULTS = ("unchanged", "new_version", "not_found")

INTERNET_FIELDS = [
    "date", "provider", "product", "technology", "network", "download_mbps", "upload_mbps",
    "price_eur_month", "promo_price_eur_month", "promo_months", "one_off_eur", "contract_months",
    "available_at_address", "promo_desc", "source_url",
]
INTERNET_REQUIRED = ["date", "provider", "price_eur_month", "source_url"]
AVAILABILITY_RANK = {"yes": 0, "unknown": 1, "no": 2}  # display order

DAILY_FIELDS = [
    "date", "status", "n_suppliers", "best_supplier", "best_product", "kwh_price",
    "fixed_supply_eur_month", "structural_annual_eur", "best_year1_supplier", "best_year1_eur",
    "avg7_eur", "avg30_eur", "avg_all_eur",
]
PERIODS = (30, 90, 180, 365)
MIN_PERIOD_COVERAGE = 0.8  # share of the window a supplier needs data for to be ranked
STALE_CHECK_DAYS = 7


class RowError(Exception):
    pass


# ---------- parsing helpers ----------

def read_csv(path, fields, required):
    """Rows as dicts with every field present; optional columns may be absent from the file."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise RowError(f"{path.name}: missing columns {missing}")
        return [{k: (r.get(k) or "").strip() for k in fields} for r in reader]


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def num(value, field, where, required=False):
    value = (value or "").replace(",", ".")
    if not value:
        if required:
            raise RowError(f"{where}: '{field}' is required")
        return None
    try:
        return float(value)
    except ValueError:
        raise RowError(f"{where}: '{field}' is not a number: {value!r}")


def parse_day(value, where):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RowError(f"{where}: bad date {value!r} (use YYYY-MM-DD)")


def parse_moment(value, where):
    """found_at must be a real ISO timestamp with timezone, e.g. from `date -Iseconds`."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise RowError(f"{where}: found_at {value!r} is not an ISO timestamp (use `date -Iseconds`)")
    if moment.tzinfo is None:
        raise RowError(f"{where}: found_at {value!r} needs a timezone offset (use `date -Iseconds`)")
    return moment


def amsterdam_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    except Exception:  # no tz database (e.g. Windows without tzdata)
        return date.today()


def supplier_lookup(cfg):
    return {s.lower(): s for s in cfg["suppliers"]}


# ---------- tariffs ----------

def effective_kwh(row, where, normal_share, fixed_costs):
    """Single-tariff price incl. energy tax and VAT, per kWh."""
    single = num(row["kwh_single"], "kwh_single", where)
    normal = num(row["kwh_normal"], "kwh_normal", where)
    dal = num(row["kwh_dal"], "kwh_dal", where)
    if single is not None:
        price = single
    elif normal is not None and dal is not None:
        price = normal_share * normal + (1 - normal_share) * dal
    else:
        raise RowError(f"{where}: need kwh_single, or both kwh_normal and kwh_dal")
    basis = row["tax_basis"] or "incl"
    if basis == "incl":
        return price
    if basis == "excl_eb":
        tax = fixed_costs.get("energy_tax_eur_kwh_incl_vat")
        if tax is None:
            raise RowError(f"{where}: tax_basis excl_eb but fixed_costs.json has no energy_tax_eur_kwh_incl_vat")
        return price + tax
    raise RowError(f"{where}: tax_basis must be 'incl' or 'excl_eb', got {basis!r}")


def monthly_fixed(row, where):
    """Fixed supply charge per month, from whichever unit the supplier published."""
    month = num(row["fixed_supply_eur_month"], "fixed_supply_eur_month", where)
    day = num(row["fixed_supply_eur_day"], "fixed_supply_eur_day", where)
    year = num(row["fixed_supply_eur_year"], "fixed_supply_eur_year", where)
    if sum(v is not None for v in (month, day, year)) > 1:
        raise RowError(f"{where}: fill only one of fixed_supply_eur_month / _day / _year")
    if day is not None:
        return day * 365 / 12
    if year is not None:
        return year / 12
    return month


def load_tariffs(rows, cfg, fixed_costs):
    """Validate rows; return {supplier: [versions sorted by valid_from]}.

    A correction for the same (supplier, valid_from) is a new row with a later found_at; the
    latest one wins. A version without a fixed charge borrows it from the supplier's previous
    version of the same calendar year (fixed charges change on 1 January at the latest).
    """
    hh, cmp_ = cfg["household"], cfg["comparison"]
    names = supplier_lookup(cfg)
    latest = {}
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        where = row.get("_where") or f"tariffs.csv row {i}"
        supplier = names.get(row["supplier"].lower())
        if supplier is None:
            raise RowError(f"{where}: unknown supplier {row['supplier']!r}. Use the exact name from "
                           f"config.json, or add a genuinely new supplier to config.json first")
        if not row["source_url"].startswith("http"):
            raise RowError(f"{where}: source_url missing or invalid")
        if row["fixed_source_url"] and not row["fixed_source_url"].startswith("http"):
            raise RowError(f"{where}: fixed_source_url invalid")
        if row["source_type"] not in SOURCE_TYPES:
            raise RowError(f"{where}: source_type must be one of {SOURCE_TYPES}")
        if row["product_kind"] not in PRODUCT_KINDS:
            raise RowError(f"{where}: product_kind must be 'modelcontract', 'standard' or empty")
        kwh = effective_kwh(row, where, hh["dual_tariff_normal_share"], fixed_costs)
        version = {
            "supplier": supplier,
            "product": row["product"],
            "valid_from": parse_day(row["valid_from"], where),
            "found_at": parse_moment(row["found_at"], where),
            "kwh_price": round(kwh, 5),
            "fixed_supply_eur_month": monthly_fixed(row, where),
            "fixed_source_url": row["fixed_source_url"] or row["source_url"],
            "fixed_carried_from": None,
            "welcome_bonus_eur": round(num(row["welcome_bonus_eur"], "welcome_bonus_eur", where) or 0.0, 2),
            "feedin_payment_eur_kwh": num(row["feedin_payment_eur_kwh"], "feedin_payment_eur_kwh", where),
            "feedin_cost_eur_kwh": num(row["feedin_cost_eur_kwh"], "feedin_cost_eur_kwh", where),
            "feedin_cost_desc": row["feedin_cost_desc"],
            "source_type": row["source_type"],
            "source_url": row["source_url"],
            "notes": row["notes"],
            "assumption": row["assumption"],
            "product_kind": row["product_kind"],
            "flagged": not (cmp_["kwh_price_sane_min"] <= kwh <= cmp_["kwh_price_sane_max"]),
        }
        key = (supplier, version["valid_from"])
        if key not in latest or version["found_at"] >= latest[key]["found_at"]:
            latest[key] = version

    by_supplier = {}
    for (supplier, _), v in sorted(latest.items(), key=lambda kv: kv[0][1]):
        by_supplier.setdefault(supplier, []).append(v)
    for versions in by_supplier.values():
        prev = None
        for v in versions:
            if v["fixed_supply_eur_month"] is None and prev and prev["fixed_supply_eur_month"] is not None \
                    and prev["valid_from"].year == v["valid_from"].year:
                v["fixed_supply_eur_month"] = prev["fixed_supply_eur_month"]
                v["fixed_source_url"] = prev["fixed_source_url"]
                v["fixed_carried_from"] = prev["valid_from"]
            v["complete"] = v["fixed_supply_eur_month"] is not None
            if v["complete"]:
                v["fixed_supply_eur_month"] = round(v["fixed_supply_eur_month"], 2)
                annual = hh["annual_kwh"] * v["kwh_price"] + 12 * v["fixed_supply_eur_month"]
                v["structural_annual_eur"] = round(annual, 2)
                v["year1_annual_eur"] = round(annual - v["welcome_bonus_eur"], 2)
            else:
                v["structural_annual_eur"] = v["year1_annual_eur"] = None
            prev = v
    return by_supplier


def manual_as_tariffs(rows):
    """Owner-entered prices (data/manual.csv) as tariff rows with source_type=manual."""
    out = []
    for i, r in enumerate(rows, start=2):
        entered = parse_day(r["entered_on"], f"manual.csv row {i}")
        out.append({f: "" for f in TARIFF_FIELDS} | {
            "supplier": r["supplier"], "product": "Variabel (entered manually)", "valid_from": r["valid_from"],
            "kwh_single": r["kwh_single"], "kwh_normal": r["kwh_normal"], "kwh_dal": r["kwh_dal"],
            "tax_basis": "incl", "fixed_supply_eur_month": r["fixed_supply_eur_month"], "welcome_bonus_eur": "0",
            "source_type": "manual", "source_url": r["source_url"],
            # Manual entries are dated, not timed: use noon. As with any correction, the latest found_at wins.
            "found_at": f"{entered.isoformat()}T12:00:00+01:00", "notes": r["notes"],
            "_where": f"manual.csv row {i}",
        })
    return out


def load_offers(rows, cfg, fixed_costs, today):
    """Fixed and dynamic offers: the newest version valid today per (supplier, contract_type)."""
    names = {s.lower(): s for s in cfg["suppliers"] + cfg.get("dynamic_suppliers", [])}
    latest = {}
    for i, row in enumerate(rows, start=2):
        where = f"offers.csv row {i}"
        supplier = names.get(row["supplier"].lower())
        if supplier is None:
            raise RowError(f"{where}: unknown supplier {row['supplier']!r} (add it to config.json suppliers or dynamic_suppliers)")
        if row["contract_type"] not in CONTRACT_TYPES:
            raise RowError(f"{where}: contract_type must be one of {CONTRACT_TYPES}")
        if row["source_type"] not in SOURCE_TYPES or not row["source_url"].startswith("http"):
            raise RowError(f"{where}: needs source_type in {SOURCE_TYPES} and an http(s) source_url")
        offer = {
            "supplier": supplier, "contract_type": row["contract_type"], "product": row["product"],
            "valid_from": parse_day(row["valid_from"], where), "found_at": parse_moment(row["found_at"], where),
            "fixed_supply_eur_month": monthly_fixed(row, where),
            "welcome_bonus_eur": num(row["welcome_bonus_eur"], "welcome_bonus_eur", where) or 0.0,
            "source_type": row["source_type"], "source_url": row["source_url"], "notes": row["notes"],
            "assumption": row["assumption"],
        }
        if row["contract_type"] == "dynamic":
            offer["dynamic_markup_eur_kwh"] = num(row["dynamic_markup_eur_kwh"], "dynamic_markup_eur_kwh", where,
                                                  required=True)
            offer["kwh_price"] = None
        else:
            offer["kwh_price"] = round(effective_kwh(row, where, cfg["household"]["dual_tariff_normal_share"],
                                                     fixed_costs), 5)
        if offer["valid_from"] > today:
            continue
        key = (supplier, offer["contract_type"])
        if key not in latest or (offer["valid_from"], offer["found_at"]) >= (latest[key]["valid_from"],
                                                                            latest[key]["found_at"]):
            latest[key] = offer
    return list(latest.values())


def version_on(versions, day):
    """The supplier's version valid on `day` (newest valid_from <= day), or None."""
    current = None
    for v in versions:
        if v["valid_from"] <= day:
            current = v
        else:
            break
    return current


def rankable(v):
    return v is not None and v["complete"] and not v["flagged"]


def deals_on(tariffs, day):
    """All suppliers' versions valid on `day`: ranked ones by annual cost, then the rest."""
    deals = [v for v in (version_on(vs, day) for vs in tariffs.values()) if v is not None]
    deals.sort(key=lambda v: (not rankable(v), v["structural_annual_eur"] or 0, v["kwh_price"]))
    return deals


# ---------- series ----------

def mean(values):
    return round(sum(values) / len(values), 2) if values else None


def daily_series(tariffs, start, end, cfg):
    min_n = cfg["comparison"]["min_suppliers_for_full_day"]
    out, full = [], []
    day = start
    while day <= end:
        ranked = [v for v in deals_on(tariffs, day) if rankable(v)]
        if ranked:
            best = ranked[0]
            best_y1 = min(ranked, key=lambda v: v["year1_annual_eur"])
            status = "full" if len(ranked) >= min_n else "partial"
            if status == "full":
                full.append((day, best["structural_annual_eur"]))
            window = lambda n: mean([val for d, val in full if day - d < timedelta(days=n)])  # noqa: E731
            out.append({
                "date": day.isoformat(), "status": status, "n_suppliers": len(ranked),
                "best_supplier": best["supplier"], "best_product": best["product"],
                "kwh_price": best["kwh_price"], "fixed_supply_eur_month": best["fixed_supply_eur_month"],
                "structural_annual_eur": best["structural_annual_eur"],
                "best_year1_supplier": best_y1["supplier"], "best_year1_eur": best_y1["year1_annual_eur"],
                "avg7_eur": window(7) if full else None, "avg30_eur": window(30) if full else None,
                "avg_all_eur": mean([val for _, val in full]),
            })
        day += timedelta(days=1)
    return out


def period_ranking(tariffs, start, end):
    """Average annual cost per supplier over the last N days: who is cheap consistently."""
    out = {}
    for n in PERIODS:
        first = max(start, end - timedelta(days=n - 1))
        span = (end - first).days + 1
        rows = []
        for supplier, versions in tariffs.items():
            costs = []
            day = first
            while day <= end:
                v = version_on(versions, day)
                if rankable(v):
                    costs.append(v["structural_annual_eur"])
                day += timedelta(days=1)
            if costs:
                rows.append({"supplier": supplier, "avg_annual_eur": mean(costs),
                             "days": len(costs), "coverage": round(len(costs) / span, 3)})
        rows.sort(key=lambda r: (r["coverage"] < MIN_PERIOD_COVERAGE, r["avg_annual_eur"]))
        out[str(n)] = {"from": first.isoformat(), "to": end.isoformat(), "days": span, "suppliers": rows}
    return out


# ---------- checks, internet, water ----------

def load_checks(rows, cfg):
    names = supplier_lookup(cfg)
    out = []
    for i, r in enumerate(rows, start=2):
        where = f"checks.csv row {i}"
        supplier = names.get(r["supplier"].lower())
        if supplier is None:
            raise RowError(f"{where}: unknown supplier {r['supplier']!r}")
        if r["result"] not in CHECK_RESULTS:
            raise RowError(f"{where}: result must be one of {CHECK_RESULTS}")
        out.append({"date": parse_day(r["date"], where), "supplier": supplier, "result": r["result"],
                    "notes": r["notes"]})
    return out


def last_checked(tariffs, checks):
    """Most recent day each supplier was looked at, from checks.csv and tariff found_at."""
    seen = {}
    for c in checks:
        seen[c["supplier"]] = max(seen.get(c["supplier"], c["date"]), c["date"])
    for supplier, versions in tariffs.items():
        for v in versions:
            d = v["found_at"].date()
            seen[supplier] = max(seen.get(supplier, d), d)
    return seen


def load_internet(rows):
    """Validate offers and compute first-year cost from the published promo terms."""
    offers = []
    for i, r in enumerate(rows, start=2):
        where = f"internet.csv row {i}"
        if not r["source_url"].startswith("http"):
            raise RowError(f"{where}: source_url missing or invalid")
        parse_day(r["date"], where)
        avail = (r["available_at_address"] or "unknown").lower()
        if avail not in AVAILABILITY_RANK:
            raise RowError(f"{where}: available_at_address must be one of {tuple(AVAILABILITY_RANK)}")
        price = num(r["price_eur_month"], "price_eur_month", where, required=True)
        promo = num(r["promo_price_eur_month"], "promo_price_eur_month", where)
        promo_months = num(r["promo_months"], "promo_months", where)
        one_off = num(r["one_off_eur"], "one_off_eur", where) or 0.0
        if promo is not None and promo_months is not None:
            pm = min(int(promo_months), 12)
            first_year = promo * pm + price * (12 - pm) + one_off
        else:
            first_year = price * 12 + one_off
        offers.append({
            "date": r["date"], "provider": r["provider"], "product": r["product"],
            "technology": r["technology"], "network": r["network"],
            "download_mbps": num(r["download_mbps"], "download_mbps", where),
            "upload_mbps": num(r["upload_mbps"], "upload_mbps", where),
            "price_eur_month": price, "promo_price_eur_month": promo, "promo_months": promo_months,
            "one_off_eur": one_off, "contract_months": num(r["contract_months"], "contract_months", where),
            "available_at_address": avail, "promo_desc": r["promo_desc"], "source_url": r["source_url"],
            "first_year_eur": round(first_year, 2),
        })
    if not offers:
        return None
    last = max(o["date"] for o in offers)
    latest = sorted((o for o in offers if o["date"] == last),
                    key=lambda o: (AVAILABILITY_RANK[o["available_at_address"]], o["first_year_eur"]))
    return {"date": last, "offers": latest}


def water_cost(water, m3):
    """Yearly water bill: fixed charge + per-m3 tariff + tap water tax (BoL), all incl. VAT."""
    if not water or water.get("fixed_eur_year") is None or water.get("eur_per_m3") is None:
        return None
    tax = water.get("tap_water_tax_eur_m3_excl_vat")
    tax_incl = tax * (1 + water.get("vat_rate", 0.09)) if tax is not None else 0.0
    return round(water["fixed_eur_year"] + (water["eur_per_m3"] + tax_incl) * m3, 2)


# ---------- assembling ----------

def load_events(rows):
    for i, r in enumerate(rows, start=2):
        parse_day(r["date"], f"events.csv row {i}")
        if r["url"] and not r["url"].startswith("http"):
            raise RowError(f"events.csv row {i}: url must start with http")
    return sorted(rows, key=lambda r: r["date"], reverse=True)


def load_runs(rows):
    out = []
    for i, r in enumerate(rows, start=2):
        where = f"runs.csv row {i}"
        start, end = parse_moment(r["started_at"], where), parse_moment(r["finished_at"], where)
        out.append({"date": parse_day(r["date"], where).isoformat(),
                    "minutes": round((end - start).total_seconds() / 60, 1),
                    "todo_before": num(r["todo_before"], "todo_before", where),
                    "todo_after": num(r["todo_after"], "todo_after", where),
                    "new_versions": num(r["new_versions"], "new_versions", where), "notes": r["notes"]})
    return out


class Data:
    """All raw inputs, validated. Raises RowError on bad data."""

    def __init__(self, data_dir, cfg, sources_path=None):
        self.cfg = cfg
        self.dir = data_dir
        self.fixed_costs = load_json(data_dir / "fixed_costs.json", {})
        self.water = load_json(data_dir / "water.json", None)
        manual = manual_as_tariffs(read_csv(data_dir / "manual.csv", MANUAL_FIELDS, MANUAL_FIELDS))
        self.tariffs = load_tariffs(read_csv(data_dir / "tariffs.csv", TARIFF_FIELDS, TARIFF_REQUIRED) + manual,
                                    cfg, self.fixed_costs)
        self.checks = load_checks(read_csv(data_dir / "checks.csv", CHECK_FIELDS, CHECK_FIELDS), cfg)
        self.internet_rows = read_csv(data_dir / "internet.csv", INTERNET_FIELDS, INTERNET_REQUIRED)
        self.internet = load_internet(self.internet_rows)
        self.offer_rows = read_csv(data_dir / "offers.csv", OFFER_FIELDS, OFFER_REQUIRED)
        load_offers(self.offer_rows, cfg, self.fixed_costs, date.max)  # validate every row
        self.events = load_events(read_csv(data_dir / "events.csv", EVENT_FIELDS, EVENT_FIELDS))
        self.calendar = load_json(data_dir / "calendar.json", [])
        self.runs = load_runs(read_csv(data_dir / "runs.csv", RUN_FIELDS, RUN_FIELDS))
        self.forecast_log = read_csv(data_dir / "forecasts.csv", FORECAST_LOG_FIELDS, FORECAST_LOG_FIELDS)
        self.sources = load_json(sources_path or (data_dir.parent / "sources.json"), {})


def version_json(v):
    return {k: (val.isoformat() if isinstance(val, (date, datetime)) else val) for k, val in v.items()}


def trusted(v):
    """Good enough to recommend: rankable, not only from a comparison site, no stated assumption."""
    return rankable(v) and v["source_type"] != "comparison" and not v["assumption"]


def advice(today_row, ranking, periods, internet, cfg):
    """Structured recommendations; the dashboard and email turn these into sentences."""
    out = {}
    if today_row:
        ranked = [v for v in ranking if rankable(v)]
        pool = [v for v in ranked if not v["assumption"]] or ranked
        best = pool[0]
        runner = pool[1] if len(pool) > 1 else None
        out["energy"] = {
            "supplier": best["supplier"], "product": best["product"], "annual_eur": best["structural_annual_eur"],
            "kwh_price": best["kwh_price"], "fixed_supply_eur_month": best["fixed_supply_eur_month"],
            "source_type": best["source_type"], "source_url": best["source_url"],
            "runner_up": runner["supplier"] if runner else None,
            "gap_eur": round(runner["structural_annual_eur"] - best["structural_annual_eur"], 2) if runner else None,
            "skipped_assumptions": [v["supplier"] for v in ranked
                                    if v["assumption"] and v["structural_annual_eur"] < best["structural_annual_eur"]],
        }
        # Longest window in which at least two suppliers have enough data: who is cheap consistently.
        for n in reversed(PERIODS):
            p = periods[str(n)]
            covered = [r for r in p["suppliers"] if r["coverage"] >= MIN_PERIOD_COVERAGE]
            if len(covered) >= 2 and p["days"] >= 14:
                out["energy"]["consistent"] = {"supplier": covered[0]["supplier"], "days": p["days"],
                                               "avg_annual_eur": covered[0]["avg_annual_eur"]}
                break
        solar = [v for v in ranking if not v["flagged"] and v["feedin_payment_eur_kwh"] is not None
                 and v["feedin_cost_eur_kwh"] is not None]
        if solar:
            s = max(solar, key=lambda v: v["feedin_payment_eur_kwh"] - v["feedin_cost_eur_kwh"])
            out["solar"] = {"supplier": s["supplier"], "feedin_payment_eur_kwh": s["feedin_payment_eur_kwh"],
                            "feedin_cost_eur_kwh": s["feedin_cost_eur_kwh"],
                            "net_eur_kwh": round(s["feedin_payment_eur_kwh"] - s["feedin_cost_eur_kwh"], 5),
                            "compared": len(solar)}
    if internet:
        min_mbps = cfg["internet"]["min_download_mbps"]
        fast_enough = [o for o in internet["offers"]
                       if (o["download_mbps"] or 0) >= min_mbps and o["available_at_address"] != "no"]
        if fast_enough:
            pick = fast_enough[0]  # sorted: confirmed availability first, then first-year cost
            out["internet"] = {**{k: pick[k] for k in (
                "provider", "product", "technology", "network", "download_mbps", "first_year_eur",
                "price_eur_month", "contract_months", "available_at_address", "source_url")},
                "min_download_mbps": min_mbps}
    return out


def month_start(d):
    return date(d.year, d.month, 1)


def announced(tariffs, today):
    """Tariff versions that start after today, with the change against the version they replace."""
    out = []
    for supplier, versions in tariffs.items():
        current = version_on(versions, today)
        for v in versions:
            if v["valid_from"] <= today:
                continue
            before = current["kwh_price"] if current else None
            out.append({"supplier": supplier, "valid_from": v["valid_from"].isoformat(), "kwh_price": v["kwh_price"],
                        "previous_kwh_price": before,
                        "change_pct": round(v["kwh_price"] / before - 1, 4) if before else None,
                        "structural_annual_eur": v["structural_annual_eur"], "source_url": v["source_url"]})
            current = v
    return sorted(out, key=lambda a: (a["valid_from"], a["supplier"]))


def first_year_start(cfg, today):
    """The forecast covers 12 months from move-in (or from next month once moved in)."""
    move_in = month_start(date.fromisoformat(cfg["household"]["move_in"]))
    return max(move_in, forecast.add_months(month_start(today), 1))


def variable_forecasts(data, today, wholesale, start):
    rows = []
    for supplier, versions in data.tariffs.items():
        current = version_on(versions, today)
        if not rankable(current):
            continue
        pt = forecast.pass_through(versions, data.cfg, wholesale)
        fy = forecast.first_year(versions, data.cfg, wholesale, start, current["fixed_supply_eur_month"],
                                 pt["lag_months"] if pt else None)
        if fy is None:
            continue
        rows.append({"supplier": supplier, "contract_type": "variable", "source_type": current["source_type"],
                     "assumption": current["assumption"], "trusted": trusted(current), "pass_through": pt,
                     "today_annual_eur": current["structural_annual_eur"], **fy})
    return sorted(rows, key=lambda r: r["base"])


def contract_options(data, today, wholesale, start):
    """Fixed 1y/3y and dynamic offers, costed over the same first year as the variable forecast."""
    cfg = data.cfg
    annual = cfg["household"]["annual_kwh"]
    rows = []
    for o in load_offers(data.offer_rows, cfg, data.fixed_costs, today):
        fixed = o["fixed_supply_eur_month"]
        row = {"supplier": o["supplier"], "contract_type": o["contract_type"], "product": o["product"],
               "valid_from": o["valid_from"].isoformat(), "kwh_price": o["kwh_price"],
               "dynamic_markup_eur_kwh": o.get("dynamic_markup_eur_kwh"), "fixed_supply_eur_month": fixed,
               "welcome_bonus_eur": o["welcome_bonus_eur"], "source_type": o["source_type"],
               "source_url": o["source_url"], "assumption": o["assumption"], "notes": o["notes"],
               "base": None, "low": None, "high": None, "last_12_months": None}
        if fixed is not None:
            if o["contract_type"] == "dynamic":
                m = o["dynamic_markup_eur_kwh"]
                paths = [forecast.dynamic_path(m, cfg, wholesale, start, 12, s) for s in (0.0, -1.0, 1.0)]
                if paths[0]:
                    row["base"], row["low"], row["high"] = (forecast.path_cost(p, cfg, start, fixed) for p in paths)
                    past = forecast.add_months(month_start(today), -12)
                    row["last_12_months"] = forecast.path_cost(
                        forecast.dynamic_path(m, cfg, wholesale, past, 12), cfg, past, fixed)
            else:
                row["base"] = row["low"] = row["high"] = round(annual * o["kwh_price"] + 12 * fixed, 2)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["contract_type"], r["base"] is None, r["base"] or 0))


def best_by_type(variable_rows, options):
    best = {}
    pool = [r for r in variable_rows if r["trusted"]] or variable_rows
    if pool:
        best["variable"] = pool[0]
    for t in CONTRACT_TYPES:
        candidates = [r for r in options if r["contract_type"] == t and r["base"] is not None]
        if candidates:
            best[t] = min(candidates, key=lambda r: r["base"])
    return best


def decision(cfg, today, variable_rows, best, mkt):
    """The recommendation card: what to choose, the expected range, confidence, and what would change it."""
    deadlines = [{"what": d["what"], "by": d["by"], "days_left": (date.fromisoformat(d["by"]) - today).days}
                 for d in cfg.get("decisions", [])]
    out = {"deadlines": deadlines, "first_year_from": None, "pick": None, "reasons": [], "watch": []}
    v = best.get("variable")
    if not v:
        return out
    pool = [r for r in variable_rows if r["trusted"]] or variable_rows
    runner = pool[1] if len(pool) > 1 else None
    width = (v["high"] - v["low"]) / v["base"] if v["base"] else 1
    n_trusted = sum(r["trusted"] for r in variable_rows)
    confidence = ("high" if width < 0.10 and n_trusted >= 6 else
                  "medium" if width < 0.25 and n_trusted >= 4 else "low")
    pick = {"contract_type": "variable", "supplier": v["supplier"], "base": v["base"], "low": v["low"],
            "high": v["high"], "confidence": confidence}
    # A fixed contract wins if it beats the expected variable cost by a clear margin.
    for t in ("fixed_1y", "fixed_3y"):
        f = best.get(t)
        if f and f["base"] < v["base"] - 50:
            pick = {"contract_type": t, "supplier": f["supplier"], "base": f["base"], "low": f["base"],
                    "high": f["base"], "confidence": confidence}
            out["reasons"].append(f"A {CONTRACT_LABELS[t]} contract from {f['supplier']} "
                                  f"(EUR {f['base']:.0f}) beats the expected variable cost (EUR {v['base']:.0f}).")
            break
    out["pick"] = pick
    if not v["trusted"]:
        out["watch"].append(f"{v['supplier']}'s price isn't confirmed by the supplier yet.")
    if runner and runner["base"] - v["base"] < 50:
        out["watch"].append(f"{runner['supplier']} is only EUR {runner['base'] - v['base']:.0f}/year more expensive; "
                            "re-check after its next price change.")
    f1 = best.get("fixed_1y")
    if pick["contract_type"] == "variable" and f1 and f1["base"] < v["high"]:
        out["watch"].append(f"If wholesale prices keep rising (high scenario EUR {v['high']:.0f}), a fixed 1-year "
                            f"contract from {f1['supplier']} at EUR {f1['base']:.0f} would be cheaper.")
    d = best.get("dynamic")
    if d and d["base"] is not None and d["base"] < pick["base"]:
        out["watch"].append(f"A dynamic contract ({d['supplier']}) is expected at EUR {d['base']:.0f}, "
                            "cheaper but with hour-to-hour price risk.")
    if mkt and mkt.get("power", {}).get("change_30d") is not None:
        ch = mkt["power"]["change_30d"]
        if abs(ch) >= 0.05:
            out["reasons"].append(f"Wholesale power is {'up' if ch > 0 else 'down'} {abs(ch):.0%} over 30 days; "
                                  "variable tariffs usually follow within 1-3 months, which the forecast includes.")
    if not v["pass_through"]:
        out["reasons"].append("The forecast uses a default 2-month delay until there's enough price history "
                              "per supplier.")
    return out


def switch_alert(data, today):
    """After moving in: alert when another supplier was cheaper by the threshold on every day of the last 60."""
    cc = data.cfg.get("current_contract") or {}
    supplier, since = cc.get("supplier"), cc.get("since")
    if not supplier or not since or supplier not in data.tariffs:
        return None
    threshold = cc.get("switch_alert_eur_year", 50)
    start = max(date.fromisoformat(since), today - timedelta(days=59))
    gaps, best_name, day = [], None, start
    while day <= today:
        mine = version_on(data.tariffs[supplier], day)
        ranked = [v for v in deals_on(data.tariffs, day) if rankable(v) and v["supplier"] != supplier]
        if not rankable(mine) or not ranked:
            return None
        gaps.append(mine["structural_annual_eur"] - ranked[0]["structural_annual_eur"])
        best_name = ranked[0]["supplier"]
        day += timedelta(days=1)
    if len(gaps) >= 60 and min(gaps) > threshold:
        return {"current": supplier, "cheaper": best_name, "saving_eur_year": round(gaps[-1], 2), "days": len(gaps)}
    return None


def help_wanted(data, today):
    """Suppliers the agent can't complete (forms, bot blocks): the owner can add them in ~2 minutes each."""
    items = []
    for s in data.cfg["suppliers"]:
        current = version_on(data.tariffs.get(s, []), today)
        if current is not None and current["complete"]:
            continue
        urls = data.sources.get("energy", {}).get(s, [])
        own = [u for u in urls if not any(x in u for x in ("keuze.nl", "selectra", "independer", "overstappen"))]
        items.append({"supplier": s, "missing": "price and fixed charge" if current is None else "fixed charge",
                      "url": (own or urls or [None])[0]})
    return {"items": items, "edit_url": f"{REPO_URL}/edit/main/data/manual.csv"}


def ops(data, today, scored):
    cfg = data.cfg
    checked = last_checked(data.tariffs, data.checks)
    current = {s: version_on(data.tariffs.get(s, []), today) for s in cfg["suppliers"]}
    ages = [(today - checked[s]).days for s in cfg["suppliers"] if s in checked]
    return {
        "suppliers_total": len(cfg["suppliers"]),
        "complete": sum(rankable(v) for v in current.values()),
        "trusted_complete": sum(trusted(v) for v in current.values() if v),
        "max_days_since_check": max(ages) if ages else None,
        "never_checked": sum(s not in checked for s in cfg["suppliers"]),
        "runs": data.runs[-7:],
        "forecast_accuracy": scored,
    }


def build(data, today, market=None):
    cfg = data.cfg
    start = date.fromisoformat(cfg["comparison"]["tracking_start"])
    history = daily_series(data.tariffs, start, today, cfg)
    today_row = history[-1] if history and history[-1]["date"] == today.isoformat() else None
    ranking = deals_on(data.tariffs, today)
    periods = period_ranking(data.tariffs, start, today)
    checked = last_checked(data.tariffs, data.checks)

    wholesale = forecast.Wholesale(market, today)
    fy_start = first_year_start(cfg, today)
    variable_rows = variable_forecasts(data, today, wholesale, fy_start)
    options = contract_options(data, today, wholesale, fy_start)
    best = best_by_type(variable_rows, options)
    upcoming = announced(data.tariffs, today)
    next_change = upcoming[0]["valid_from"] if upcoming else None
    next_ranking = None
    if next_change:
        nd = date.fromisoformat(next_change)
        next_ranking = {"date": next_change, "ranking": [
            {"supplier": v["supplier"], "kwh_price": v["kwh_price"], "structural_annual_eur": v["structural_annual_eur"]}
            for v in deals_on(data.tariffs, nd) if rankable(v)][:5]}
    scored = forecast.score(data.forecast_log, data.tariffs, today)

    budget = None
    grid = data.fixed_costs.get("grid_costs_eur_year")
    refund = data.fixed_costs.get("energy_tax_refund_eur_year")
    if today_row and grid is not None and refund is not None:
        budget = {"supply_eur_year": today_row["structural_annual_eur"], "grid_costs_eur_year": grid,
                  "energy_tax_refund_eur_year": refund,
                  "total_eur_year": round(today_row["structural_annual_eur"] + grid - refund, 2)}
        water_year = water_cost(data.water, cfg["household"]["annual_water_m3"])
        if water_year is not None:
            budget["water_eur_year"] = water_year

    research_today = (any(c["date"] == today for c in data.checks)
                      or any(v["found_at"].date() == today for vs in data.tariffs.values() for v in vs))
    stale = sorted(s for s in cfg["suppliers"]
                   if s not in checked or (today - checked[s]).days > STALE_CHECK_DAYS)
    warnings = []
    if not research_today:
        warnings.append("The research agent recorded nothing today; prices shown are the latest known versions.")
    if stale:
        warnings.append(f"Not checked in the last {STALE_CHECK_DAYS} days (or never): {', '.join(stale)}.")
    if today_row and today_row["status"] == "partial":
        warnings.append(f"Only {today_row['n_suppliers']} suppliers have a complete price today; "
                        "today doesn't count towards the averages.")
    if market is None:
        warnings.append("Wholesale market data couldn't be loaded; forecasts assume flat prices.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "site_url": SITE_URL,
        "household": cfg["household"],
        "comparison": cfg["comparison"],
        "internet_config": {k: cfg["internet"][k] for k in ("min_download_mbps", "address_networks")},
        "today": today_row,
        "ranking": [{**version_json(v), "rankable": rankable(v), "trusted": trusted(v),
                     "last_checked": checked[v["supplier"]].isoformat() if v["supplier"] in checked else None}
                    for v in ranking],
        "history": history,
        "periods": periods,
        "announced": upcoming,
        "next_ranking": next_ranking,
        "calendar": sorted((e for e in data.calendar if e.get("date", "") >= today.isoformat()),
                           key=lambda e: e["date"]),
        "events": data.events[:12],
        "market": market,
        "forecast": {"first_year_from": fy_start.isoformat(), "variable": variable_rows},
        "contracts": {"options": options, "best": best},
        "decision": decision(cfg, today, variable_rows, best, market) | {"first_year_from": fy_start.isoformat()},
        "switch_alert": switch_alert(data, today),
        "help_wanted": help_wanted(data, today),
        "ops": ops(data, today, scored),
        "fixed_costs": data.fixed_costs,
        "water": data.water,
        "budget": budget,
        "internet": data.internet,
        "advice": advice(today_row, ranking, periods, data.internet, cfg),
        "health": {"research_today": research_today, "stale_suppliers": stale, "warnings": warnings},
    }


def todo(data, today):
    """What today's research run still has to do, most important first."""
    cfg = data.cfg
    items = []
    checked = last_checked(data.tariffs, data.checks)
    month_start_window = today.day <= 3  # variable tariffs usually change on the 1st
    blocked = data.sources.get("blocked", {})
    for s in cfg["suppliers"]:
        current = version_on(data.tariffs.get(s, []), today)
        last = checked.get(s)
        hint = " [website blocks automated browsers: use the document routes in ROUTINE.md]" if s in blocked else ""
        if current is None:
            items.append(f"NO TARIFF: {s} has no tariff version valid today{hint}")
        elif not current["complete"]:
            items.append(f"NO FIXED CHARGE: {s} (current version valid from {current['valid_from']}){hint}")
        elif current["source_type"] == "comparison":
            items.append(f"UPGRADE SOURCE: {s} is only sourced from a comparison site; find the official tariff sheet")
        elif current["assumption"]:
            items.append(f"RESOLVE ASSUMPTION: {s} ({current['assumption']})")
        if current is not None and (last is None or (today - last).days >= STALE_CHECK_DAYS
                                    or (month_start_window and last < today)):
            why = "start of the month" if month_start_window else f"last checked {last}"
            items.append(f"CHECK FOR NEW VERSION: {s} ({why})")

    recent = {o["provider"].lower() for o in data.internet_rows
              if o["date"] >= (today - timedelta(days=7)).isoformat()}
    done_today = any(o["date"] == today.isoformat() for o in data.internet_rows)
    if len(recent) < 5 or (today.weekday() == 0 and not done_today):
        items.append(f"INTERNET CHECK DUE: {len(recent)} providers recorded in the last 7 days "
                     "(need >= 5, and a fresh check every Monday)")

    week_ago = today - timedelta(days=7)
    for t, need in (("fixed_1y", 4), ("fixed_3y", 3), ("dynamic", 4)):
        fresh = {r["supplier"] for r in data.offer_rows
                 if r["contract_type"] == t and r["found_at"][:10] >= week_ago.isoformat()}
        if len(fresh) < need:
            items.append(f"OFFERS CHECK DUE: {t} offers from {len(fresh)} suppliers in the last 7 days (need >= {need})")

    for name in ("fixed_costs.json", "water.json"):
        stamp = load_json(data.dir / name, {}).get("checked_at")
        if not stamp or (today - date.fromisoformat(stamp)).days > 31:
            items.append(f"MONTHLY CHECK DUE: {name} (checked_at {stamp or 'missing'})")
    next_year = str(today.year + 1)
    if today.month >= 10 and next_year not in cfg["taxes"]["energy_tax_eur_kwh_ex_vat"]:
        items.append(f"TAX TABLE: add the {next_year} electricity energy tax (1st bracket, excl. VAT) to config.json "
                     "taxes once published (Belastingplan), and the matching calendar.json event")

    tracking = date.fromisoformat(cfg["comparison"]["tracking_start"])
    for s in cfg["suppliers"]:
        versions = data.tariffs.get(s, [])
        if versions and min(v["valid_from"] for v in versions) >= tracking:
            items.append(f"BACKFILL (low priority, only if time is left): {s} has no tariff history before "
                         f"{tracking}; add earlier versions back to 2025-01 from its archive or news")
    return items


def log_forecast(data, today, market):
    """Append today's 1-3 month price forecasts to data/forecasts.csv (once per day)."""
    path = data.dir / "forecasts.csv"
    if any(r["made_on"] == today.isoformat() for r in data.forecast_log):
        return 0
    wholesale = forecast.Wholesale(market, today)
    eligible = [s for s, vs in data.tariffs.items() if trusted(version_on(vs, today))]
    rows = forecast.forecast_rows(data.tariffs, data.cfg, wholesale, today, eligible)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FORECAST_LOG_FIELDS, lineterminator="\n")
        if new_file:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--site-src", default=str(ROOT / "site"))
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--todo", action="store_true", help="print today's research checklist")
    mode.add_argument("--check", action="store_true", help="validate and print today's summary")
    mode.add_argument("--out", help="validate and build the site into this directory")
    mode.add_argument("--log-forecast", action="store_true", help="append today's forecasts to data/forecasts.csv")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for tests")
    ap.add_argument("--market-file", help="use this market JSON instead of calling the API")
    ap.add_argument("--no-market", action="store_true", help="skip wholesale market data")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    today = date.fromisoformat(args.today) if args.today else amsterdam_today()
    try:
        data = Data(Path(args.data_dir), cfg)
    except RowError as e:
        print(f"VALIDATION ERROR: {e}", file=sys.stderr)
        return 1

    if args.todo:
        items = todo(data, today)
        print(f"TODO for {today} ({today.strftime('%A')}): {len(items)} item(s)")
        for item in items:
            print(f"- {item}")
        if not items:
            print("- nothing left: every supplier is complete, official and checked; no checks are due")
        return 0

    if args.no_market:
        mkt = None
    elif args.market_file:
        mkt = json.loads(Path(args.market_file).read_text(encoding="utf-8"))
    else:
        mkt = market_data.load(today)

    if args.log_forecast:
        print(f"forecasts logged: {log_forecast(data, today, mkt)}")
        return 0

    dash = build(data, today, mkt)
    t = dash["today"]
    if t:
        print(f"{t['date']} [{t['status']}, {t['n_suppliers']} suppliers] best: {t['best_supplier']} "
              f"EUR {t['structural_annual_eur']}/yr | avg7 {t['avg7_eur']} avg30 {t['avg30_eur']} all {t['avg_all_eur']}")
    else:
        print(f"{today}: no supplier has a complete tariff yet.")
    pick = dash["decision"]["pick"]
    if pick:
        print(f"forecast first year from {dash['decision']['first_year_from']}: {pick['supplier']} ({pick['contract_type']}) "
              f"EUR {pick['base']:.0f} (range {pick['low']:.0f}-{pick['high']:.0f}, {pick['confidence']} confidence)")
    for v in dash["ranking"]:
        if v["flagged"]:
            print(f"FLAGGED (price out of range): {v['supplier']} {v['kwh_price']} EUR/kWh", file=sys.stderr)
    for w in dash["health"]["warnings"]:
        print(f"NOTE: {w}")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for f in Path(args.site_src).iterdir():
            if f.is_file():
                shutil.copy(f, out / f.name)
        (out / "data").mkdir(exist_ok=True)
        for f in Path(args.data_dir).iterdir():  # raw data stays downloadable for transparency
            if f.suffix in (".csv", ".json"):
                shutil.copy(f, out / "data" / f.name)
        with (out / "data" / "daily_best.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=DAILY_FIELDS, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            w.writerows(dash["history"])
        (out / "dashboard.json").write_text(json.dumps(dash, indent=1, ensure_ascii=False, default=str),
                                            encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
