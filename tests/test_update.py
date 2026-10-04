"""Run: python -m unittest discover tests"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import email_digest  # noqa: E402
import privacy_check  # noqa: E402
import update  # noqa: E402

SUPPLIERS = ["A", "B", "C", "D", "E", "F", "G", "H"]
CFG = {
    "household": {"location": "Test", "annual_kwh": 3500, "dual_tariff_normal_share": 0.6, "annual_water_m3": 90},
    "internet": {"min_download_mbps": 100, "address_networks": []},
    "comparison": {"tracking_start": "2026-10-01", "min_suppliers_for_full_day": 5,
                   "kwh_price_sane_min": 0.15, "kwh_price_sane_max": 0.45},
    "suppliers": SUPPLIERS,
}


def tariff(supplier, valid_from, kwh="0.25", fixed="6", found_at=None, **kw):
    r = {f: "" for f in update.TARIFF_FIELDS}
    r.update(supplier=supplier, product="Variabel", valid_from=valid_from, kwh_single=kwh, tax_basis="incl",
             fixed_supply_eur_month=fixed, welcome_bonus_eur="0", source_type="official",
             source_url=f"https://example.com/{supplier}", found_at=found_at or f"{valid_from}T07:00:00+02:00")
    r.update(kw)
    return r


def write_csv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def internet_row(**kw):
    return {k: "" for k in update.INTERNET_FIELDS} | {"date": "2026-10-05", "product": "x", "technology": "fiber",
                                                      "source_url": "https://example.com"} | kw


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.data = self.dir / "data"
        self.data.mkdir()
        (self.dir / "config.json").write_text(json.dumps(CFG))
        (self.data / "fixed_costs.json").write_text(json.dumps({
            "checked_at": "2026-10-01", "grid_costs_eur_year": 400.0,
            "energy_tax_refund_eur_year": 600.0, "energy_tax_eur_kwh_incl_vat": 0.11}))
        write_csv(self.data / "checks.csv", update.CHECK_FIELDS, [])

    def tearDown(self):
        self.tmp.cleanup()

    def tariffs(self, rows):
        write_csv(self.data / "tariffs.csv", update.TARIFF_FIELDS, rows)

    def load(self):
        return update.Data(self.data, CFG)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "update.py"), "--data-dir", str(self.data),
                               "--config", str(self.dir / "config.json"), *args], capture_output=True, text=True)


class TariffVersions(Base):
    def test_versions_fill_every_day_and_averages(self):
        # A: 0.20 from 1 Oct, 0.30 from 4 Oct (then B at 0.25 becomes cheapest). C..E fill to 5 suppliers.
        self.tariffs([tariff("A", "2026-10-01", "0.20"), tariff("A", "2026-10-04", "0.30"),
                      tariff("B", "2026-10-01", "0.25")] + [tariff(s, "2026-10-01", "0.28") for s in "CDE"])
        dash = update.build(self.load(), date(2026, 10, 5))
        h = dash["history"]
        self.assertEqual([x["date"] for x in h], ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"])
        self.assertTrue(all(x["status"] == "full" for x in h))
        a_cost, b_cost = 3500 * 0.20 + 72, 3500 * 0.25 + 72  # 772, 947
        self.assertEqual([x["best_supplier"] for x in h], ["A", "A", "A", "B", "B"])
        self.assertAlmostEqual(h[-1]["avg_all_eur"], round((3 * a_cost + 2 * b_cost) / 5, 2))
        self.assertEqual(dash["today"]["best_supplier"], "B")

    def test_correction_with_later_found_at_wins(self):
        self.tariffs([tariff("A", "2026-10-01", "0.30", found_at="2026-10-02T09:00:00+02:00"),
                      tariff("A", "2026-10-01", "0.20", found_at="2026-10-02T08:00:00+02:00")])
        self.assertEqual(self.load().tariffs["A"][0]["kwh_price"], 0.30)

    def test_fixed_charge_carried_within_year_only(self):
        self.tariffs([tariff("A", "2026-07-01", fixed="7"), tariff("A", "2026-10-01", fixed=""),
                      tariff("A", "2027-01-01", fixed="")])
        v = self.load().tariffs["A"]
        self.assertEqual(v[1]["fixed_supply_eur_month"], 7.0)
        self.assertEqual(v[1]["fixed_carried_from"], date(2026, 7, 1))
        self.assertFalse(v[2]["complete"])  # never carried across 1 January

    def test_fixed_charge_units(self):
        self.tariffs([tariff("A", "2026-10-01", fixed="", fixed_supply_eur_day="0.36"),
                      tariff("B", "2026-10-01", fixed="", fixed_supply_eur_year="120")])
        t = self.load().tariffs
        self.assertAlmostEqual(t["A"][0]["fixed_supply_eur_month"], round(0.36 * 365 / 12, 2))
        self.assertAlmostEqual(t["B"][0]["fixed_supply_eur_month"], 10.0)

    def test_dual_tariff_and_excl_energy_tax(self):
        self.tariffs([tariff("A", "2026-10-01", kwh="", kwh_normal="0.14", kwh_dal="0.10", tax_basis="excl_eb")])
        self.assertAlmostEqual(self.load().tariffs["A"][0]["kwh_price"], 0.6 * 0.14 + 0.4 * 0.10 + 0.11)

    def test_validation_errors(self):
        cases = [
            (tariff("Typo", "2026-10-01"), "unknown supplier"),
            (tariff("A", "2026-10-01", found_at="2026-10-01 12:00"), "timezone"),
            (tariff("A", "2026-10-01", found_at="noon"), "not an ISO timestamp"),
            (tariff("A", "2026-10-01", source_type="blog"), "source_type"),
            (tariff("A", "2026-10-01", source_url=""), "source_url"),
            (tariff("A", "2026-10-01", fixed="6", fixed_supply_eur_year="72"), "only one"),
        ]
        for row, message in cases:
            with self.subTest(message=message):
                self.tariffs([row])
                with self.assertRaisesRegex(update.RowError, message):
                    self.load()

    def test_cli_validation_error_writes_nothing(self):
        self.tariffs([tariff("A", "2026-10-01", source_url="")])
        out = self.dir / "_site"
        res = self.run_cli("--out", str(out), "--today", "2026-10-05")
        self.assertEqual(res.returncode, 1)
        self.assertIn("VALIDATION ERROR", res.stderr)
        self.assertFalse(out.exists())

    def test_flagged_and_incomplete_are_not_ranked(self):
        self.tariffs([tariff("A", "2026-10-01", "0.05"), tariff("B", "2026-10-01", "0.20", fixed=""),
                      tariff("C", "2026-10-01", "0.30")])
        dash = update.build(self.load(), date(2026, 10, 1))
        self.assertEqual(dash["today"]["best_supplier"], "C")
        self.assertEqual([r["supplier"] for r in dash["ranking"] if r["rankable"]], ["C"])


class Periods(Base):
    def test_period_ranking_needs_coverage(self):
        # A is cheap but only appears on 5 Oct; B covers the whole period.
        self.tariffs([tariff("A", "2026-10-05", "0.20"), tariff("B", "2026-10-01", "0.25")])
        p = update.build(self.load(), date(2026, 10, 5))["periods"]["30"]
        self.assertEqual(p["days"], 5)
        self.assertEqual(p["suppliers"][0]["supplier"], "B")  # A has 1/5 coverage, so ranks after B
        self.assertEqual(p["suppliers"][1]["coverage"], 0.2)


class Todo(Base):
    def test_checklist(self):
        self.tariffs([tariff("A", "2026-10-01", found_at="2026-10-01T08:00:00+02:00"),
                      tariff("B", "2026-10-01", fixed=""),
                      tariff("C", "2026-10-01", source_type="comparison")])
        (self.data / "water.json").write_text(json.dumps({"checked_at": "2026-08-01"}))
        text = "\n".join(update.todo(self.load(), date(2026, 10, 9)))
        self.assertIn("NO TARIFF: D", text)
        self.assertIn("NO FIXED CHARGE: B", text)
        self.assertIn("UPGRADE SOURCE: C", text)
        self.assertIn("CHECK FOR NEW VERSION: A (last checked 2026-10-01)", text)
        self.assertIn("INTERNET CHECK DUE: 0 providers", text)
        self.assertIn("MONTHLY CHECK DUE: water.json", text)
        self.assertNotIn("fixed_costs.json", text)

    def test_checks_csv_counts_as_checked(self):
        self.tariffs([tariff(s, "2026-10-01") for s in SUPPLIERS])
        write_csv(self.data / "checks.csv", update.CHECK_FIELDS,
                  [{"date": "2026-10-09", "supplier": s, "result": "unchanged", "notes": ""} for s in SUPPLIERS])
        text = "\n".join(update.todo(self.load(), date(2026, 10, 9)))
        self.assertNotIn("CHECK FOR NEW VERSION", text)
        self.assertNotIn("NO ", text)

    def test_month_start_requires_fresh_check(self):
        self.tariffs([tariff("A", "2026-10-01", found_at="2026-10-30T08:00:00+01:00")])
        self.assertIn("CHECK FOR NEW VERSION: A (start of the month)",
                      "\n".join(update.todo(self.load(), date(2026, 11, 1))))

    def test_cli_todo_writes_nothing(self):
        self.tariffs([])
        res = self.run_cli("--todo", "--today", "2026-10-04")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("TODO for 2026-10-04 (Sunday)", res.stdout)


class InternetAndAdvice(Base):
    def test_first_year_sorting_and_pick(self):
        self.tariffs([])
        write_csv(self.data / "internet.csv", update.INTERNET_FIELDS, [
            internet_row(provider="Fiber", download_mbps="500", price_eur_month="50", promo_price_eur_month="30",
                         promo_months="6", one_off_eur="25", available_at_address="yes"),
            internet_row(provider="Maybe", download_mbps="200", price_eur_month="35", available_at_address="unknown"),
            internet_row(provider="Slow", download_mbps="50", price_eur_month="20", available_at_address="yes"),
            internet_row(provider="Nope", download_mbps="1000", price_eur_month="10", available_at_address="no"),
        ])
        dash = update.build(self.load(), date(2026, 10, 5))
        offers = dash["internet"]["offers"]
        self.assertEqual([o["provider"] for o in offers], ["Slow", "Fiber", "Maybe", "Nope"])
        self.assertAlmostEqual(offers[1]["first_year_eur"], 30 * 6 + 50 * 6 + 25)
        self.assertEqual(dash["advice"]["internet"]["provider"], "Fiber")

    def test_solar_advice_uses_net_feedin(self):
        self.tariffs([tariff("A", "2026-10-01", feedin_payment_eur_kwh="0.12", feedin_cost_eur_kwh="0.04"),
                      tariff("B", "2026-10-01", feedin_payment_eur_kwh="0.15", feedin_cost_eur_kwh="0.14")])
        s = update.build(self.load(), date(2026, 10, 5))["advice"]["solar"]
        self.assertEqual(s["supplier"], "A")
        self.assertAlmostEqual(s["net_eur_kwh"], 0.08)

    def test_water_cost(self):
        w = {"fixed_eur_year": 56.68, "eur_per_m3": 1.34, "vat_rate": 0.09, "tap_water_tax_eur_m3_excl_vat": 0.437}
        self.assertAlmostEqual(update.water_cost(w, 90), round(56.68 + (1.34 + 0.437 * 1.09) * 90, 2))

    def test_health_warns_when_agent_did_not_run(self):
        self.tariffs([tariff(s, "2026-10-01", found_at="2026-10-01T08:00:00+02:00") for s in SUPPLIERS])
        h = update.build(self.load(), date(2026, 10, 3))["health"]
        self.assertFalse(h["research_today"])
        self.assertTrue(any("recorded nothing today" in w for w in h["warnings"]))


class Build(Base):
    def test_out_writes_site(self):
        self.tariffs([tariff(s, "2026-10-01") for s in SUPPLIERS])
        site_src = self.dir / "site"
        site_src.mkdir()
        (site_src / "index.html").write_text("<title>t</title>")
        out = self.dir / "_site"
        res = self.run_cli("--out", str(out), "--site-src", str(site_src), "--today", "2026-10-02")
        self.assertEqual(res.returncode, 0, res.stderr)
        for name in ("index.html", "dashboard.json", "data/tariffs.csv", "data/daily_best.csv"):
            self.assertTrue((out / name).exists(), name)


class Email(unittest.TestCase):
    def dash(self, today=900.0, yesterday=950.0):
        day = lambda d, v: {"date": d, "status": "full", "n_suppliers": 5, "best_supplier": "A",  # noqa: E731
                            "best_product": "Variabel", "kwh_price": 0.2, "fixed_supply_eur_month": 6.0,
                            "structural_annual_eur": v, "best_year1_supplier": "A", "best_year1_eur": v,
                            "avg7_eur": 925.0, "avg30_eur": 925.0, "avg_all_eur": 925.0}
        return {"today_date": "2026-10-05", "today": day("2026-10-05", today),
                "history": [day("2026-10-04", yesterday), day("2026-10-05", today)],
                "ranking": [{"supplier": "A", "structural_annual_eur": today, "rankable": True, "source_type": "official"}],
                "advice": {}, "budget": None, "health": {"warnings": ["W1"]}}

    def test_compose_mentions_change_average_warning_and_link(self):
        subject, lines = email_digest.compose(self.dash(), "https://site")
        text = "\n".join(lines)
        self.assertIn("5 Oct", subject)
        self.assertIn("−€ 50 vs yesterday", subject)
        self.assertIn("2.7% cheaper than the 30-day average", text)
        self.assertIn("W1", text)
        self.assertIn("https://site", text)

    def test_alert_when_build_failed(self):
        subject, lines = email_digest.alert("https://run", "https://site")
        self.assertIn("failed", subject)
        self.assertIn("https://run", "\n".join(lines))


class Privacy(unittest.TestCase):
    def test_finds_pattern_and_reports_location_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.csv").write_text("nothing here\n")
            (root / "bad.csv").write_text("line one\nSecret Street 12\n")
            hits = privacy_check.find([root / "ok.csv", root / "bad.csv"], r"secret\s*street", root)
            self.assertEqual(hits, ["bad.csv:2"])


if __name__ == "__main__":
    unittest.main()
