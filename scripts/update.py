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
    "tax_basis", "fixed_supply_eur_month", "welcome_bonus_eur",
    "feedin_payment_eur_kwh", "feedin_cost_desc", "source_url", "checked_at", "notes",
]
INTERNET_FIELDS = [
    "date", "provider", "product", "technology", "download_mbps",
    "price_eur_month", "first_year_cost_eur", "contract_months", "promo_desc", "source_url",
]
DAILY_BEST_FIELDS = [
    "date", "status", "n_suppliers", "best_supplier", "best_product", "kwh_price",
    "fixed_supply_eur_month", "structural_annual_eur", "year1_annual_eur",
    "best_year1_supplier", "best_year1_eur", "avg7_eur", "avg30_eur", "avg_all_eur",
]


class RowError(Exception):
    pass


def read_csv(path, fields):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in fields if c not in (reader.fieldnames or [])]
        if missing:
            raise RowError(f"{path.name}: missing columns {missing}")
        return list(reader)


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


def load_energy(rows, cfg, fixed_costs):
    """Validate raw rows; keep the latest check per (date, supplier)."""
    hh, cmp_ = cfg["household"], cfg["comparison"]
    latest = {}
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        if not (row["source_url"] or "").startswith("http"):
            raise RowError(f"row {i}: source_url missing or invalid")
        d = parse_date(row["date"], i)
        supplier = (row["supplier"] or "").strip()
        if not supplier:
            raise RowError(f"row {i}: supplier is required")
        kwh = effective_kwh_price(row, i, hh["dual_tariff_normal_share"], fixed_costs)
        fixed = num(row["fixed_supply_eur_month"], "fixed_supply_eur_month", i, required=True)
        bonus = num(row["welcome_bonus_eur"], "welcome_bonus_eur", i) or 0.0
        structural = hh["annual_kwh"] * kwh + 12 * fixed
        deal = {
            "date": d,
            "supplier": supplier,
            "product": row["product"].strip(),
            "kwh_price": round(kwh, 5),
            "fixed_supply_eur_month": round(fixed, 2),
            "welcome_bonus_eur": round(bonus, 2),
            "structural_annual_eur": round(structural, 2),
            "year1_annual_eur": round(structural - bonus, 2),
            "feedin_payment_eur_kwh": num(row["feedin_payment_eur_kwh"], "feedin_payment_eur_kwh", i),
            "feedin_cost_desc": row["feedin_cost_desc"].strip(),
            "source_url": row["source_url"].strip(),
            "checked_at": row["checked_at"].strip(),
            "notes": row["notes"].strip(),
            "flagged": not (cmp_["kwh_price_sane_min"] <= kwh <= cmp_["kwh_price_sane_max"]),
        }
        key = (d, supplier.lower())
        if key not in latest or deal["checked_at"] >= latest[key]["checked_at"]:
            latest[key] = deal
    by_date = {}
    for deal in latest.values():
        by_date.setdefault(deal["date"], []).append(deal)
    for deals in by_date.values():
        deals.sort(key=lambda x: (x["flagged"], x["structural_annual_eur"]))
    return by_date


def mean(values):
    return round(sum(values) / len(values), 2) if values else None


def daily_best(by_date, cfg):
    min_n = cfg["comparison"]["min_suppliers_for_full_day"]
    out = []
    full = []  # (date, best structural) for non-partial days
    for d in sorted(by_date):
        ranked = [x for x in by_date[d] if not x["flagged"]]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "docs" / "data"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()
    data = Path(args.data_dir)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    fixed_costs = load_json(data / "fixed_costs.json", {})
    water = load_json(data / "water.json", None)

    try:
        by_date = load_energy(read_csv(data / "energy.csv", ENERGY_FIELDS), cfg, fixed_costs)
        internet_rows = read_csv(data / "internet.csv", INTERNET_FIELDS)
    except RowError as e:
        print(f"VALIDATION ERROR: {e}", file=sys.stderr)
        return 1

    history = daily_best(by_date, cfg)
    write_csv(data / "daily_best.csv", DAILY_BEST_FIELDS, history)

    latest_date = max(by_date) if by_date else None
    today_deals = [deal_json(x) for x in by_date.get(latest_date, [])]

    internet_latest = None
    if internet_rows:
        last = max(r["date"] for r in internet_rows)
        internet_latest = {
            "date": last,
            "offers": sorted(
                (r for r in internet_rows if r["date"] == last),
                key=lambda r: float(r["first_year_cost_eur"] or r["price_eur_month"] or 0),
            ),
        }

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
    if budget is not None and water and water.get("fixed_eur_year") is not None and water.get("eur_per_m3") is not None:
        budget["water_eur_year"] = round(
            water["fixed_eur_year"] + water["eur_per_m3"] * cfg["household"]["annual_water_m3"], 2)

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
    }
    (data / "dashboard.json").write_text(json.dumps(dashboard, indent=1, ensure_ascii=False), encoding="utf-8")

    if history:
        t = history[-1]
        print(f"{t['date']} [{t['status']}, {t['n_suppliers']} suppliers] best: {t['best_supplier']} "
              f"EUR {t['structural_annual_eur']}/yr | avg7 {t['avg7_eur']} avg30 {t['avg30_eur']} all {t['avg_all_eur']}")
        flagged = [x for x in by_date[latest_date] if x["flagged"]]
        for x in flagged:
            print(f"FLAGGED (price out of range): {x['supplier']} {x['kwh_price']} EUR/kWh", file=sys.stderr)
    else:
        print("No energy data yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
