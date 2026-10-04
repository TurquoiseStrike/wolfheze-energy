"""Recompute daily best deals, averages and dashboard.json from the raw CSVs.

All arithmetic lives here so the numbers never depend on the research agent
doing math. Standard library only.

Usage: python scripts/update.py [--data-dir docs/data] [--config config.json]
Exit code 1 means a raw row failed validation; nothing is written in that case.
"""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENERGY_FIELDS = [
    "date", "supplier", "product", "kwh_single", "kwh_normal", "kwh_dal",
    "tax_basis", "fixed_supply_eur_month", "fixed_supply_eur_day", "fixed_supply_eur_year", "welcome_bonus_eur",
    "feedin_payment_eur_kwh", "feedin_cost_eur_kwh", "feedin_cost_desc", "source_url", "fixed_source_url",
    "checked_at", "notes",
]
ENERGY_REQUIRED = ["date", "supplier", "source_url"]
# Fixed supply charges usually change only on 1 Jan / 1 Jul, so a sourced value
# from an earlier day may stand in when today's research didn't find one.
FIXED_CARRY_DAYS = 92
INTERNET_FIELDS = [
    "date", "provider", "product", "technology", "download_mbps", "upload_mbps",
    "price_eur_month", "promo_price_eur_month", "promo_months", "one_off_eur", "contract_months",
    "available_at_address", "promo_desc", "source_url",
]
INTERNET_REQUIRED = ["date", "provider", "price_eur_month", "source_url"]
AVAILABILITY = ("yes", "no", "unknown")
AVAILABILITY_RANK = {"yes": 0, "unknown": 1, "no": 2}  # display order: confirmed, unconfirmed, unavailable
DAILY_BEST_FIELDS = [
    "date", "status", "n_suppliers", "best_supplier", "best_product", "kwh_price",
    "fixed_supply_eur_month", "structural_annual_eur", "year1_annual_eur",
    "best_year1_supplier", "best_year1_eur", "avg7_eur", "avg30_eur", "avg_all_eur",
]


class RowError(Exception):
    pass


def read_csv(path, fields, required):
    """Rows as dicts with every field present; optional columns may be absent from the file."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise RowError(f"{path.name}: missing columns {missing}")
        return [{k: (r.get(k) or "") for k in fields} for r in reader]


def num(value, field, row_no, required=False):
    value = (value or "").strip().replace(",", ".")
    if not value:
        if required:
            raise RowError(f"row {row_no}: '{field}' is required")
        return None
    try:
        return float(value)
    except ValueError:
        raise RowError(f"row {row_no}: '{field}' is not a number: {value!r}")


def parse_date(value, row_no):
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise RowError(f"row {row_no}: bad date {value!r} (use YYYY-MM-DD)")


def effective_kwh_price(row, row_no, normal_share, fixed_costs):
    """Single-tariff price incl. energy tax and VAT, per kWh."""
    single = num(row["kwh_single"], "kwh_single", row_no)
    normal = num(row["kwh_normal"], "kwh_normal", row_no)
    dal = num(row["kwh_dal"], "kwh_dal", row_no)
    if single is not None:
        price = single
    elif normal is not None and dal is not None:
        price = normal_share * normal + (1 - normal_share) * dal
    else:
        raise RowError(f"row {row_no}: need kwh_single, or both kwh_normal and kwh_dal")

    basis = (row["tax_basis"] or "incl").strip()
    if basis == "incl":
        return price
    if basis == "excl_eb":
        # Supplier listed price incl. VAT but excl. energy tax: add tax incl. VAT.
        tax = fixed_costs.get("energy_tax_eur_kwh_incl_vat")
        if tax is None:
            raise RowError(f"row {row_no}: tax_basis excl_eb but fixed_costs.json has no energy_tax_eur_kwh_incl_vat")
        return price + tax
    raise RowError(f"row {row_no}: tax_basis must be 'incl' or 'excl_eb', got {basis!r}")


def monthly_fixed(row, row_no):
    """Fixed supply charge per month, from whichever unit the supplier published."""
    month = num(row["fixed_supply_eur_month"], "fixed_supply_eur_month", row_no)
    day = num(row["fixed_supply_eur_day"], "fixed_supply_eur_day", row_no)
    year = num(row["fixed_supply_eur_year"], "fixed_supply_eur_year", row_no)
    if sum(v is not None for v in (month, day, year)) > 1:
        raise RowError(f"row {row_no}: fill only one of fixed_supply_eur_month / _day / _year")
    if day is not None:
        return day * 365 / 12
    if year is not None:
        return year / 12
    return month


def load_energy(rows, cfg, fixed_costs):
    """Validate raw rows; keep the latest check per (date, supplier)."""
    hh, cmp_ = cfg["household"], cfg["comparison"]
    latest = {}
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        if not (row["source_url"] or "").startswith("http"):
            raise RowError(f"row {i}: source_url missing or invalid")
        fixed_url = (row["fixed_source_url"] or "").strip()
        if fixed_url and not fixed_url.startswith("http"):
            raise RowError(f"row {i}: fixed_source_url invalid")
        d = parse_date(row["date"], i)
        supplier = (row["supplier"] or "").strip()
        if not supplier:
            raise RowError(f"row {i}: supplier is required")
        deal = {
            "date": d,
            "supplier": supplier,
            "product": row["product"].strip(),
            "kwh_price": round(effective_kwh_price(row, i, hh["dual_tariff_normal_share"], fixed_costs), 5),
            "fixed_supply_eur_month": monthly_fixed(row, i),
            "fixed_source_url": fixed_url or row["source_url"].strip(),
            "fixed_carried_from": None,
            "welcome_bonus_eur": round(num(row["welcome_bonus_eur"], "welcome_bonus_eur", i) or 0.0, 2),
            "feedin_payment_eur_kwh": num(row["feedin_payment_eur_kwh"], "feedin_payment_eur_kwh", i),
            "feedin_cost_eur_kwh": num(row["feedin_cost_eur_kwh"], "feedin_cost_eur_kwh", i),
            "feedin_cost_desc": row["feedin_cost_desc"].strip(),
            "source_url": row["source_url"].strip(),
            "checked_at": row["checked_at"].strip(),
            "notes": row["notes"].strip(),
        }
        key = (d, supplier.lower())
        if key not in latest or deal["checked_at"] >= latest[key]["checked_at"]:
            latest[key] = deal

    # Fill missing fixed charges from the supplier's most recent sourced value.
    last_fixed = {}
    for key in sorted(latest):
        deal = latest[key]
        if deal["fixed_supply_eur_month"] is not None:
            last_fixed[key[1]] = deal
        else:
            prev = last_fixed.get(key[1])
            if prev and (deal["date"] - prev["date"]).days <= FIXED_CARRY_DAYS:
                deal["fixed_supply_eur_month"] = prev["fixed_supply_eur_month"]
                deal["fixed_source_url"] = prev["fixed_source_url"]
                deal["fixed_carried_from"] = prev["date"].isoformat()

    by_date = {}
    for deal in latest.values():
        kwh, fixed = deal["kwh_price"], deal["fixed_supply_eur_month"]
        deal["incomplete"] = fixed is None
        deal["flagged"] = not (cmp_["kwh_price_sane_min"] <= kwh <= cmp_["kwh_price_sane_max"])
        if fixed is None:
            deal["structural_annual_eur"] = deal["year1_annual_eur"] = None
        else:
            deal["fixed_supply_eur_month"] = round(fixed, 2)
            structural = hh["annual_kwh"] * kwh + 12 * fixed
            deal["structural_annual_eur"] = round(structural, 2)
            deal["year1_annual_eur"] = round(structural - deal["welcome_bonus_eur"], 2)
        by_date.setdefault(deal["date"], []).append(deal)
    for deals in by_date.values():
        # Ranked deals first, then incomplete ones (no fixed charge) by kWh price, flagged last.
        deals.sort(key=lambda x: (x["flagged"], x["incomplete"], x["structural_annual_eur"] or 0, x["kwh_price"]))
    return by_date


def mean(values):
    return round(sum(values) / len(values), 2) if values else None


def daily_best(by_date, cfg):
    min_n = cfg["comparison"]["min_suppliers_for_full_day"]
    out = []
    full = []  # (date, best structural) for non-partial days
    for d in sorted(by_date):
        ranked = [x for x in by_date[d] if not x["flagged"] and not x["incomplete"]]
        if not ranked:
            continue
        best = ranked[0]
        best_y1 = min(ranked, key=lambda x: x["year1_annual_eur"])
        status = "full" if len(ranked) >= min_n else "partial"
        if status == "full":
            full.append((d, best["structural_annual_eur"]))

        def window(days):
            return mean([v for fd, v in full if d - fd < timedelta(days=days)])

        out.append({
            "date": d.isoformat(),
            "status": status,
            "n_suppliers": len(ranked),
            "best_supplier": best["supplier"],
            "best_product": best["product"],
            "kwh_price": best["kwh_price"],
            "fixed_supply_eur_month": best["fixed_supply_eur_month"],
            "structural_annual_eur": best["structural_annual_eur"],
            "year1_annual_eur": best["year1_annual_eur"],
            "best_year1_supplier": best_y1["supplier"],
            "best_year1_eur": best_y1["year1_annual_eur"],
            "avg7_eur": window(7) if full else None,
            "avg30_eur": window(30) if full else None,
            "avg_all_eur": mean([v for _, v in full]),
        })
    return out


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def deal_json(x):
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in x.items()}


def load_internet(rows):
    """Validate offers and compute first-year cost from the published promo terms."""
    offers = []
    for i, r in enumerate(rows, start=2):
        if not r["source_url"].startswith("http"):
            raise RowError(f"internet row {i}: source_url missing or invalid")
        parse_date(r["date"], i)
        avail = (r["available_at_address"] or "unknown").strip().lower()
        if avail not in AVAILABILITY:
            raise RowError(f"internet row {i}: available_at_address must be one of {AVAILABILITY}")
        price = num(r["price_eur_month"], "price_eur_month", i, required=True)
        promo = num(r["promo_price_eur_month"], "promo_price_eur_month", i)
        promo_months = num(r["promo_months"], "promo_months", i)
        one_off = num(r["one_off_eur"], "one_off_eur", i) or 0.0
        if promo is not None and promo_months is not None:
            pm = min(int(promo_months), 12)
            first_year = promo * pm + price * (12 - pm) + one_off
        else:
            first_year = price * 12 + one_off
        offers.append({
            "date": r["date"].strip(), "provider": r["provider"].strip(), "product": r["product"].strip(),
            "technology": r["technology"].strip(),
            "download_mbps": num(r["download_mbps"], "download_mbps", i),
            "upload_mbps": num(r["upload_mbps"], "upload_mbps", i),
            "price_eur_month": price, "promo_price_eur_month": promo, "promo_months": promo_months,
            "one_off_eur": one_off, "contract_months": num(r["contract_months"], "contract_months", i),
            "available_at_address": avail, "promo_desc": r["promo_desc"].strip(),
            "source_url": r["source_url"].strip(), "first_year_eur": round(first_year, 2),
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


def advice(history, today_deals, internet, cfg):
    """Structured recommendations; the dashboard turns these into sentences."""
    out = {}
    if history:
        t = history[-1]
        recent = history[-30:]
        ranked = [x for x in today_deals
                  if x["date"] == t["date"] and not x["flagged"] and not x["incomplete"]]
        runner = ranked[1] if len(ranked) > 1 else None
        out["energy"] = {
            "supplier": t["best_supplier"], "product": t["best_product"], "annual_eur": t["structural_annual_eur"],
            "kwh_price": t["kwh_price"], "fixed_supply_eur_month": t["fixed_supply_eur_month"],
            "days_best": sum(h["best_supplier"] == t["best_supplier"] for h in recent), "days_tracked": len(recent),
            "runner_up": runner["supplier"] if runner else None,
            "gap_eur": round(runner["structural_annual_eur"] - t["structural_annual_eur"], 2) if runner else None,
            "source_url": next((x["source_url"] for x in ranked if x["supplier"] == t["best_supplier"]), None),
        }
        # Solar: compare what you keep per kWh fed back (payment minus feed-in fee). Only suppliers
        # with a flat per-kWh fee are comparable; tiered fees stay in the table as text.
        solar = [x for x in today_deals if x["date"] == t["date"] and not x["flagged"]
                 and x["feedin_payment_eur_kwh"] is not None and x["feedin_cost_eur_kwh"] is not None]
        if solar:
            s = max(solar, key=lambda x: x["feedin_payment_eur_kwh"] - x["feedin_cost_eur_kwh"])
            out["solar"] = {"supplier": s["supplier"], "feedin_payment_eur_kwh": s["feedin_payment_eur_kwh"],
                            "feedin_cost_eur_kwh": s["feedin_cost_eur_kwh"],
                            "net_eur_kwh": round(s["feedin_payment_eur_kwh"] - s["feedin_cost_eur_kwh"], 5),
                            "compared": len(solar)}
    if internet:
        min_mbps = cfg["internet"]["min_download_mbps"]
        fast_enough = [o for o in internet["offers"]
                       if (o["download_mbps"] or 0) >= min_mbps and o["available_at_address"] != "no"]
        if fast_enough:
            pick = fast_enough[0]  # already sorted: confirmed availability first, then first-year cost
            out["internet"] = {**{k: pick[k] for k in ("provider", "product", "technology", "download_mbps",
                                                        "first_year_eur", "price_eur_month", "contract_months",
                                                        "available_at_address", "source_url")},
                               "min_download_mbps": min_mbps}
    return out


def amsterdam_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    except Exception:  # no tz database (e.g. Windows without tzdata)
        return date.today()


def todo(cfg, data, today):
    """What today's run still has to do. Printed for the research agent; changes nothing."""
    items = []
    energy = read_csv(data / "energy.csv", ENERGY_FIELDS, ENERGY_REQUIRED)
    fixed_costs = load_json(data / "fixed_costs.json", {})
    by_date = load_energy(energy, cfg, fixed_costs)
    todays = {x["supplier"].lower(): x for x in by_date.get(today, [])}
    for s in cfg["suppliers"]:
        if s.lower() not in todays:
            items.append(f"ENERGY MISSING: {s} has no row for {today}")
    for x in todays.values():
        if x["incomplete"]:
            items.append(f"ENERGY NO FIXED CHARGE: {x['supplier']} (find its modelcontract tariff sheet)")

    internet = read_csv(data / "internet.csv", INTERNET_FIELDS, INTERNET_REQUIRED)
    week_ago = (today - timedelta(days=7)).isoformat()
    recent = {r["provider"].lower() for r in internet if r["date"] >= week_ago}
    done_today = any(r["date"] == today.isoformat() for r in internet)
    if len(recent) < 5 or (today.weekday() == 0 and not done_today):
        items.append(f"INTERNET CHECK DUE: {len(recent)} providers recorded in the last 7 days (need >= 5, and a fresh check every Monday)")

    for name in ("fixed_costs.json", "water.json"):
        checked = load_json(data / name, {}).get("checked_at")
        if not checked or (today - date.fromisoformat(checked)).days > 31:
            items.append(f"MONTHLY CHECK DUE: {name} (checked_at {checked or 'missing'})")
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "docs" / "data"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--todo", action="store_true", help="print what today's run still has to do, then exit")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for tests")
    args = ap.parse_args()
    data = Path(args.data_dir)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    fixed_costs = load_json(data / "fixed_costs.json", {})
    water = load_json(data / "water.json", None)

    if args.todo:
        today = date.fromisoformat(args.today) if args.today else amsterdam_today()
        try:
            items = todo(cfg, data, today)
        except RowError as e:
            print(f"VALIDATION ERROR: {e}", file=sys.stderr)
            return 1
        print(f"TODO for {today} ({today.strftime('%A')}): {len(items)} item(s)")
        for item in items:
            print(f"- {item}")
        if not items:
            print("- nothing left: every supplier has a complete row today and no checks are due")
        return 0

    try:
        by_date = load_energy(read_csv(data / "energy.csv", ENERGY_FIELDS, ENERGY_REQUIRED), cfg, fixed_costs)
        internet_latest = load_internet(read_csv(data / "internet.csv", INTERNET_FIELDS, INTERNET_REQUIRED))
    except RowError as e:
        print(f"VALIDATION ERROR: {e}", file=sys.stderr)
        return 1

    history = daily_best(by_date, cfg)
    write_csv(data / "daily_best.csv", DAILY_BEST_FIELDS, history)

    latest_date = max(by_date) if by_date else None
    today_deals = [deal_json(x) for x in by_date.get(latest_date, [])]

    budget = None
    if history and fixed_costs:
        latest = history[-1]
        grid = fixed_costs.get("grid_costs_eur_year")
        refund = fixed_costs.get("energy_tax_refund_eur_year")
        if grid is not None and refund is not None:
            budget = {
                "supply_eur_year": latest["structural_annual_eur"],
                "grid_costs_eur_year": grid,
                "energy_tax_refund_eur_year": refund,
                "total_eur_year": round(latest["structural_annual_eur"] + grid - refund, 2),
            }
    water_year = water_cost(water, cfg["household"]["annual_water_m3"])
    if budget is not None and water_year is not None:
        budget["water_eur_year"] = water_year

    dashboard = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "household": cfg["household"],
        "comparison": cfg["comparison"],
        "latest_date": latest_date.isoformat() if latest_date else None,
        "today": history[-1] if history else None,
        "today_deals": today_deals,
        "history": history,
        "fixed_costs": fixed_costs,
        "water": water,
        "budget": budget,
        "internet": internet_latest,
        "advice": advice(history, today_deals, internet_latest, cfg),
    }
    (data / "dashboard.json").write_text(json.dumps(dashboard, indent=1, ensure_ascii=False), encoding="utf-8")

    if history:
        t = history[-1]
        print(f"{t['date']} [{t['status']}, {t['n_suppliers']} suppliers] best: {t['best_supplier']} "
              f"EUR {t['structural_annual_eur']}/yr | avg7 {t['avg7_eur']} avg30 {t['avg30_eur']} all {t['avg_all_eur']}")
    else:
        print("No ranked energy data yet.")
    for x in by_date.get(latest_date, []):
        if x["flagged"]:
            print(f"FLAGGED (price out of range): {x['supplier']} {x['kwh_price']} EUR/kWh", file=sys.stderr)
        elif x["incomplete"]:
            print(f"INCOMPLETE (no fixed charge, not ranked): {x['supplier']}", file=sys.stderr)
        elif x["fixed_carried_from"]:
            print(f"fixed charge carried from {x['fixed_carried_from']}: {x['supplier']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
