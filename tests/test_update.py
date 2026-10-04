"""Run: python -m unittest discover tests

Builds a throwaway data dir with fictional prices, runs update.py on it,
and checks rankings and averages against hand-computed values.
"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update  # noqa: E402

SUPPLIERS = ["A", "B", "C", "D", "E", "F", "G", "H"]


def row(d, supplier, kwh="0.25", fixed="6", **kw):
    r = {f: "" for f in update.ENERGY_FIELDS}
    r.update(date=d, supplier=supplier, product="Variabel", kwh_single=kwh, tax_basis="incl",
             fixed_supply_eur_month=fixed, welcome_bonus_eur="0",
             source_url="https://example.com/" + supplier, checked_at=d + "T07:00:00")
    r.update(kw)
    return r


def write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=update.ENERGY_FIELDS)
        w.writeheader()
        w.writerows(rows)


def run(data_dir):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "update.py"), "--data-dir", str(data_dir)],
                          capture_output=True, text=True)


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "fixed_costs.json").write_text(json.dumps({
            "checked_at": "2026-10-01", "grid_costs_eur_year": 400.0,
            "energy_tax_refund_eur_year": 600.0, "energy_tax_eur_kwh_incl_vat": 0.11}))

    def tearDown(self):
        self.tmp.cleanup()

    def history(self):
        with (self.dir / "daily_best.csv").open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_ranking_averages_and_partial_days(self):
        rows = []
        start = date(2026, 10, 1)
        # Days 0..9: supplier A is cheapest at 0.20 + day*0.01. Day 5 only has 3 suppliers (partial).
        for i in range(10):
            d = (start + timedelta(days=i)).isoformat()
            for s in (SUPPLIERS[:3] if i == 5 else SUPPLIERS):
                kwh = 0.20 + i * 0.01 if s == "A" else 0.30
                rows.append(row(d, s, kwh=f"{kwh:.2f}"))
        write(self.dir / "energy.csv", rows)
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)

        h = self.history()
        self.assertEqual(len(h), 10)
        self.assertEqual(h[5]["status"], "partial")
        # Day i best = 3500*(0.20+0.01i) + 72 = 772 + 35i
        best = [772 + 35 * i for i in range(10)]
        for i in range(10):
            self.assertAlmostEqual(float(h[i]["structural_annual_eur"]), best[i], places=2)
        full = [b for i, b in enumerate(best) if i != 5]
        self.assertAlmostEqual(float(h[9]["avg_all_eur"]), round(sum(full) / len(full), 2))
        last7 = [best[i] for i in range(3, 10) if i != 5]  # days 3..9, minus the partial day
        self.assertAlmostEqual(float(h[9]["avg7_eur"]), round(sum(last7) / len(last7), 2))

        dash = json.loads((self.dir / "dashboard.json").read_text(encoding="utf-8"))
        self.assertEqual(dash["today"]["best_supplier"], "A")
        self.assertAlmostEqual(dash["budget"]["total_eur_year"], best[9] + 400 - 600)

    def test_dual_tariff_excl_tax_bonus_flag_and_dedupe(self):
        d = "2026-10-01"
        rows = [row(d, s, kwh="0.30") for s in SUPPLIERS[2:]]
        # B: dual tariff excl. energy tax -> 0.6*0.14 + 0.4*0.10 + 0.11 = 0.234
        rows.append(row(d, "B", kwh="", kwh_normal="0.14", kwh_dal="0.10", tax_basis="excl_eb"))
        # A: absurd price -> flagged, excluded
        rows.append(row(d, "A", kwh="0.05"))
        # C re-checked later the same day with a big bonus -> later check wins
        rows.append(row(d, "C", kwh="0.30", welcome_bonus_eur="400", checked_at=d + "T09:00:00"))
        write(self.dir / "energy.csv", rows)
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("FLAGGED", res.stderr)

        h = self.history()[0]
        self.assertEqual(h["best_supplier"], "B")
        self.assertAlmostEqual(float(h["kwh_price"]), 0.234, places=5)
        self.assertEqual(h["best_year1_supplier"], "C")
        self.assertAlmostEqual(float(h["best_year1_eur"]), 3500 * 0.30 + 72 - 400)
        self.assertEqual(h["n_suppliers"], "7")  # 8 suppliers minus flagged A
        self.assertEqual(h["status"], "partial")  # 7 < min 8

    def test_fixed_charge_carry_forward_and_incomplete(self):
        rows = [row("2026-10-01", s) for s in SUPPLIERS]
        rows += [row("2026-10-02", s, fixed="") for s in SUPPLIERS[:7]]  # A..G: fixed missing today
        rows.append(row("2026-10-02", "H", fixed="5", fixed_source_url="https://example.com/H-pdf"))
        rows.append(row("2026-10-02", "New", kwh="0.20", fixed=""))  # cheapest kWh, but no fixed -> not ranked
        rows.append(row("2027-01-15", "A", fixed=""))  # > 92 days later -> no carry
        write(self.dir / "energy.csv", rows)
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("INCOMPLETE", res.stderr)

        h = {r["date"]: r for r in self.history()}
        day2 = h["2026-10-02"]
        self.assertEqual(day2["status"], "full")  # 7 carried + H
        self.assertEqual(day2["best_supplier"], "H")
        self.assertNotIn("2027-01-15", h)  # A's only row that day is incomplete

        dash = json.loads((self.dir / "dashboard.json").read_text(encoding="utf-8"))
        a = next(x for x in dash["today_deals"] if x["supplier"] == "A")
        self.assertTrue(a["incomplete"])
        self.assertIsNone(a["structural_annual_eur"])

    def test_fixed_charge_units_and_solar_advice(self):
        d = "2026-10-01"
        rows = [row(d, s) for s in SUPPLIERS[2:]]
        rows.append(row(d, "A", fixed="", fixed_supply_eur_day="0.36", feedin_payment_eur_kwh="0.12", feedin_cost_eur_kwh="0.04"))
        rows.append(row(d, "B", fixed="", fixed_supply_eur_year="120", feedin_payment_eur_kwh="0.15", feedin_cost_eur_kwh="0.14"))
        write(self.dir / "energy.csv", rows)
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        dash = json.loads((self.dir / "dashboard.json").read_text(encoding="utf-8"))
        by = {x["supplier"]: x for x in dash["today_deals"]}
        self.assertAlmostEqual(by["A"]["fixed_supply_eur_month"], round(0.36 * 365 / 12, 2))
        self.assertAlmostEqual(by["B"]["fixed_supply_eur_month"], 10.0)
        # B pays more per kWh, but A keeps more after the fee.
        self.assertEqual(dash["advice"]["solar"]["supplier"], "A")
        self.assertAlmostEqual(dash["advice"]["solar"]["net_eur_kwh"], 0.08)
        # C..H at 6/mo (EUR 947/yr) beat B at 10/mo and A at ~10.95/mo; ties keep input order.
        self.assertEqual(dash["advice"]["energy"]["supplier"], "C")
        self.assertEqual(dash["advice"]["energy"]["runner_up"], "D")
        self.assertEqual(dash["advice"]["energy"]["gap_eur"], 0)

    def test_two_fixed_units_rejected(self):
        write(self.dir / "energy.csv", [row("2026-10-01", "A", fixed="6", fixed_supply_eur_year="72")])
        res = run(self.dir)
        self.assertEqual(res.returncode, 1)
        self.assertIn("only one", res.stderr)

    def test_internet_first_year_sorting_and_advice(self):
        write(self.dir / "energy.csv", [])
        fields = update.INTERNET_FIELDS
        offers = [
            # 30/mo for 6 months then 50, plus 25 one-off -> 180 + 300 + 25 = 505
            {"provider": "Fiber", "download_mbps": "500", "price_eur_month": "50", "promo_price_eur_month": "30",
             "promo_months": "6", "one_off_eur": "25", "available_at_address": "yes"},
            # cheapest, but availability unknown -> sorted after confirmed offers
            {"provider": "Maybe", "download_mbps": "200", "price_eur_month": "35", "available_at_address": "unknown"},
            # too slow for the advice
            {"provider": "Slow", "download_mbps": "50", "price_eur_month": "20", "available_at_address": "yes"},
            {"provider": "Nope", "download_mbps": "1000", "price_eur_month": "10", "available_at_address": "no"},
        ]
        with (self.dir / "internet.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for o in offers:
                w.writerow({k: "" for k in fields} | {"date": "2026-10-05", "product": "x", "technology": "fiber",
                                                      "source_url": "https://example.com"} | o)
        (self.dir / "water.json").write_text(json.dumps({"fixed_eur_year": 56.68, "eur_per_m3": 1.34,
                                                         "vat_rate": 0.09, "tap_water_tax_eur_m3_excl_vat": 0.437}))
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        dash = json.loads((self.dir / "dashboard.json").read_text(encoding="utf-8"))
        offers = dash["internet"]["offers"]
        self.assertEqual([o["provider"] for o in offers], ["Slow", "Fiber", "Maybe", "Nope"])
        self.assertAlmostEqual(offers[1]["first_year_eur"], 505.0)
        self.assertEqual(dash["advice"]["internet"]["provider"], "Fiber")
        self.assertAlmostEqual(update.water_cost(json.loads((self.dir / "water.json").read_text()), 90),
                               round(56.68 + (1.34 + 0.437 * 1.09) * 90, 2))

    def test_missing_source_url_fails_without_writing(self):
        write(self.dir / "energy.csv", [row("2026-10-01", "A", source_url="")])
        res = run(self.dir)
        self.assertEqual(res.returncode, 1)
        self.assertIn("source_url", res.stderr)
        self.assertFalse((self.dir / "dashboard.json").exists())

    def test_empty_data(self):
        write(self.dir / "energy.csv", [])
        res = run(self.dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        dash = json.loads((self.dir / "dashboard.json").read_text(encoding="utf-8"))
        self.assertIsNone(dash["today"])


if __name__ == "__main__":
    unittest.main()
