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
import forecast  # noqa: E402
import privacy_check  # noqa: E402
import update  # noqa: E402

SUPPLIERS = ["A", "B", "C", "D", "E", "F", "G", "H"]
CFG = {
    "household": {"location": "Test", "annual_kwh": 3500, "dual_tariff_normal_share": 0.6, "annual_water_m3": 90,
                  "monthly_profile": [1 / 12] * 12, "move_in": "2026-12-01"},
    "internet": {"min_download_mbps": 100, "address_networks": []},
    "comparison": {"tracking_start": "2026-10-01", "min_suppliers_for_full_day": 5,
                   "kwh_price_sane_min": 0.15, "kwh_price_sane_max": 0.45},
    "decisions": [{"what": "Sign", "by": "2026-11-15"}],
    "current_contract": {"supplier": None, "since": None, "switch_alert_eur_year": 50},
    "taxes": {"vat": 0.21, "energy_tax_eur_kwh_ex_vat": {"2026": 0.09}},
    "suppliers": SUPPLIERS,
    "dynamic_suppliers": ["Dyn"],
}


def flat_market(level=0.10, months=("2026-06", "2026-07", "2026-08", "2026-09"), vol=0.1, monthly=None):
    """A market dict like market.load() returns, with a flat (or given) monthly wholesale price."""
    monthly = monthly or {m: level for m in months}
    return {"power": {"monthly": [{"month": m, "avg": v} for m, v in monthly.items()],
                      "avg_30d": level, "monthly_volatility": vol, "change_30d": 0.0}}


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


class ManualAndAssumptions(Base):
    def test_manual_entry_completes_a_supplier(self):
        self.tariffs([tariff("A", "2026-10-01", fixed="")])
        write_csv(self.data / "manual.csv", update.MANUAL_FIELDS, [{
            "supplier": "A", "valid_from": "2026-10-01", "kwh_single": "0.25", "kwh_normal": "", "kwh_dal": "",
            "fixed_supply_eur_month": "8", "source_url": "https://a.nl/tarieven", "entered_on": "2026-10-05", "notes": ""}])
        v = self.load().tariffs["A"][0]
        self.assertEqual(v["source_type"], "manual")
        self.assertEqual(v["fixed_supply_eur_month"], 8.0)

    def test_manual_errors_point_to_manual_csv(self):
        self.tariffs([])
        write_csv(self.data / "manual.csv", update.MANUAL_FIELDS, [{
            "supplier": "Typo", "valid_from": "2026-10-01", "kwh_single": "0.25", "kwh_normal": "", "kwh_dal": "",
            "fixed_supply_eur_month": "8", "source_url": "https://a.nl", "entered_on": "2026-10-05", "notes": ""}])
        with self.assertRaisesRegex(update.RowError, "manual.csv row 2"):
            self.load()

    def test_assumption_ranks_but_is_not_recommended(self):
        self.tariffs([tariff("A", "2026-10-01", "0.20", assumption="VAT unclear"), tariff("B", "2026-10-01", "0.25")])
        dash = update.build(self.load(), date(2026, 10, 5))
        self.assertEqual(dash["today"]["best_supplier"], "A")
        self.assertEqual(dash["advice"]["energy"]["supplier"], "B")
        self.assertEqual(dash["advice"]["energy"]["skipped_assumptions"], ["A"])
        self.assertIn("RESOLVE ASSUMPTION: A", "\n".join(update.todo(self.load(), date(2026, 10, 5))))


class Announced(Base):
    def test_future_version_is_announced_with_next_ranking(self):
        self.tariffs([tariff("A", "2026-10-01", "0.20"), tariff("A", "2026-11-01", "0.30"),
                      tariff("B", "2026-10-01", "0.25")])
        dash = update.build(self.load(), date(2026, 10, 5))
        self.assertEqual(dash["today"]["best_supplier"], "A")  # the announced price isn't active yet
        a = dash["announced"][0]
        self.assertEqual((a["supplier"], a["valid_from"]), ("A", "2026-11-01"))
        self.assertAlmostEqual(a["change_pct"], 0.5)
        self.assertEqual(dash["next_ranking"]["ranking"][0]["supplier"], "B")


class Forecasts(unittest.TestCase):
    def versions(self, *pairs):
        return [{"valid_from": date.fromisoformat(d), "kwh_price": p} for d, p in pairs]

    def test_flat_market_keeps_the_price(self):
        w = forecast.Wholesale(flat_market(), date(2026, 10, 5))
        path = forecast.variable_path(self.versions(("2026-10-01", 0.30)), CFG, w, date(2026, 12, 1), 12)
        self.assertTrue(all(abs(p - 0.30) < 1e-9 for p in path))

    def test_wholesale_rise_is_passed_on_after_the_lag(self):
        # Wholesale was 0.10 until Sept and is 0.15 now: with a 2-month lag the rise reaches December.
        mkt = flat_market(level=0.15, monthly={"2026-07": 0.10, "2026-08": 0.10, "2026-09": 0.10})
        w = forecast.Wholesale(mkt, date(2026, 10, 5))
        path = forecast.variable_path(self.versions(("2026-10-01", 0.30)), CFG, w, date(2026, 11, 1), 2, lag=2)
        self.assertAlmostEqual(path[0], 0.30)  # Nov follows Sept wholesale: unchanged
        self.assertAlmostEqual(path[1], 0.30 + 0.05 * 1.21)  # Dec follows Oct: +0.05 ex VAT

    def test_announced_version_anchors_the_path(self):
        w = forecast.Wholesale(flat_market(), date(2026, 10, 5))
        path = forecast.variable_path(self.versions(("2026-10-01", 0.30), ("2026-12-01", 0.35)), CFG, w,
                                      date(2026, 11, 1), 3)
        self.assertAlmostEqual(path[0], 0.30)
        self.assertAlmostEqual(path[1], 0.35)

    def test_first_year_band_and_monthly_profile(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["household"]["monthly_profile"] = [0.5] + [0.5 / 11] * 11
        w = forecast.Wholesale(flat_market(), date(2026, 10, 5))
        fy = forecast.first_year(self.versions(("2026-10-01", 0.30)), cfg, w, date(2026, 12, 1), 6.0)
        self.assertAlmostEqual(fy["base"], round(3500 * 0.30 + 72, 2))
        self.assertLess(fy["low"], fy["base"])
        self.assertGreater(fy["high"], fy["base"])

    def test_pass_through_finds_the_lag(self):
        # Supplier supply price = wholesale two months earlier + 0.03 margin. The wholesale series must be
        # irregular: with a straight-line trend every lag fits equally well and the lag can't be identified.
        zigzag = [0.08, 0.12, 0.07, 0.15, 0.09, 0.14, 0.10, 0.16, 0.11]
        w_by_month = {f"2026-{m:02d}": zigzag[m - 1] for m in range(1, 10)}
        w = forecast.Wholesale(flat_market(level=0.15, monthly=w_by_month), date(2026, 10, 5))
        versions = []
        for m in range(3, 10):
            supply = w_by_month[f"2026-{m - 2:02d}"] + 0.03
            versions.append({"valid_from": date(2026, m, 1), "kwh_price": (supply + 0.09) * 1.21})
        pt = forecast.pass_through(versions, CFG, w)
        self.assertEqual(pt["lag_months"], 2)
        self.assertAlmostEqual(pt["margin"], 0.03, places=4)

    def test_dynamic_path(self):
        w = forecast.Wholesale(flat_market(level=0.10), date(2026, 10, 5))
        path = forecast.dynamic_path(0.02, CFG, w, date(2026, 12, 1), 1)
        self.assertAlmostEqual(path[0], (0.10 + 0.09) * 1.21 + 0.02)

    def test_score(self):
        tariffs = {"A": [{"valid_from": date(2026, 11, 1), "kwh_price": 0.30}]}
        rows = [{"made_on": "2026-10-05", "supplier": "A", "target_month": "2026-11", "kwh_base": "0.33",
                 "kwh_low": "0.25", "kwh_high": "0.35"},
                {"made_on": "2026-10-05", "supplier": "A", "target_month": "2026-12", "kwh_base": "0.30",
                 "kwh_low": "0.29", "kwh_high": "0.31"}]
        s = forecast.score(rows, tariffs, date(2026, 11, 2))
        self.assertEqual(s, [{"horizon_months": 1, "n": 1, "mape": 0.1, "band_hit_rate": 1.0}])  # Dec not yet due


class OffersAndDecision(Base):
    def offers(self, rows):
        write_csv(self.data / "offers.csv", update.OFFER_FIELDS, rows)

    def offer(self, supplier, contract_type, **kw):
        r = {f: "" for f in update.OFFER_FIELDS}
        r.update(supplier=supplier, contract_type=contract_type, product="x", valid_from="2026-10-01",
                 tax_basis="incl", fixed_supply_eur_month="6", welcome_bonus_eur="0", source_type="official",
                 source_url="https://example.com", found_at="2026-10-05T08:00:00+02:00")
        r.update(kw)
        return r

    def test_fixed_offer_beats_variable_and_is_picked(self):
        self.tariffs([tariff(s, "2026-10-01", "0.30") for s in "ABCDEF"])
        self.offers([self.offer("A", "fixed_1y", kwh_single="0.25"),
                     self.offer("Dyn", "dynamic", dynamic_markup_eur_kwh="0.02")])
        dash = update.build(self.load(), date(2026, 10, 5), flat_market())
        best = dash["contracts"]["best"]
        self.assertAlmostEqual(best["fixed_1y"]["base"], 3500 * 0.25 + 72)
        self.assertIsNotNone(best["dynamic"]["base"])
        self.assertEqual(dash["decision"]["pick"]["contract_type"], "fixed_1y")

    def test_offer_validation(self):
        self.tariffs([])
        self.offers([self.offer("Nobody", "fixed_1y", kwh_single="0.25")])
        with self.assertRaisesRegex(update.RowError, "unknown supplier"):
            self.load()
        self.offers([self.offer("A", "fixed_9y", kwh_single="0.25")])
        with self.assertRaisesRegex(update.RowError, "contract_type"):
            self.load()

    def test_switch_alert_after_two_months(self):
        self.tariffs([tariff("A", "2026-10-01", "0.35"), tariff("B", "2026-10-01", "0.25")])
        cfg = json.loads(json.dumps(CFG))
        cfg["current_contract"] = {"supplier": "A", "since": "2026-10-01", "switch_alert_eur_year": 50}
        data = update.Data(self.data, cfg)
        self.assertIsNone(update.switch_alert(data, date(2026, 11, 15)))  # only 46 days
        alert = update.switch_alert(data, date(2026, 12, 15))
        self.assertEqual(alert["cheaper"], "B")

    def test_log_forecast_once_per_day(self):
        self.tariffs([tariff("A", "2026-10-01", "0.30")])
        data = self.load()
        self.assertEqual(update.log_forecast(data, date(2026, 10, 5), flat_market()), 3)
        self.assertEqual(update.log_forecast(self.load(), date(2026, 10, 5), flat_market()), 0)

    def test_todo_has_offers_tax_and_backfill(self):
        self.tariffs([tariff("A", "2026-10-01")])
        text = "\n".join(update.todo(self.load(), date(2026, 10, 5)))
        self.assertIn("OFFERS CHECK DUE: fixed_1y", text)
        self.assertIn("TAX TABLE: add the 2027", text)
        self.assertIn("BACKFILL (low priority", text)

    def test_help_wanted_lists_incomplete_suppliers(self):
        self.tariffs([tariff("A", "2026-10-01", fixed="")])
        h = update.help_wanted(self.load(), date(2026, 10, 5))
        missing = {i["supplier"]: i["missing"] for i in h["items"]}
        self.assertEqual(missing["A"], "fixed charge")
        self.assertEqual(missing["B"], "price and fixed charge")
        self.assertTrue(h["edit_url"].endswith("/edit/main/data/manual.csv"))


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

    def test_brevo_request(self):
        req = email_digest.brevo_request("KEY", "from@x.nl", ["a@x.nl", "b@y.nl"], "Subj", ["Line", "", "Dashboard: u"], "u")
        body = json.loads(req.data)
        self.assertEqual(req.full_url, email_digest.BREVO_URL)
        self.assertEqual(req.get_header("Api-key"), "KEY")
        self.assertEqual(body["sender"]["email"], "from@x.nl")
        self.assertEqual([t["email"] for t in body["to"]], ["a@x.nl", "b@y.nl"])
        self.assertIn("Open the dashboard", body["htmlContent"])

    def test_not_configured_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            dash = Path(tmp) / "dashboard.json"
            dash.write_text(json.dumps(self.dash()))
            env = {k: v for k, v in __import__("os").environ.items()
                   if k not in ("BREVO_API_KEY", "EMAIL_FROM", "EMAIL_TO")}
            res = subprocess.run([sys.executable, str(ROOT / "scripts" / "email_digest.py"), "--dashboard", str(dash),
                                  "--site-url", "u"], capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("not configured", res.stdout)

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
