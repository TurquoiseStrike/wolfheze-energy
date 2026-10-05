"""Send the daily email digest built from dashboard.json, via Brevo's transactional email API.

  python scripts/email_digest.py --dashboard _site/dashboard.json --site-url https://... [--dry-run]

Environment (GitHub Actions secrets):
  BREVO_API_KEY   Brevo API key (SMTP & API > API keys)
  EMAIL_FROM      a sender address verified in Brevo (Senders & IP)
  EMAIL_TO        comma-separated recipients
  BUILD_OK        "true" unless today's build failed; then an alert is sent instead
  RUN_URL         link to the Actions run, used in the alert
Without BREVO_API_KEY / EMAIL_FROM / EMAIL_TO the script prints a notice and exits 0 (email not configured).
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
LABELS = {"variable": "variable", "fixed_1y": "fixed 1-year", "fixed_3y": "fixed 3-year", "dynamic": "dynamic"}


def eur(v, decimals=0):
    if v is None:
        return "—"
    s = f"{abs(v):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'−' if v < 0 else ''}€ {s}"


def per_kwh(v):
    return f"€ {v:.4f}".replace(".", ",")


def short(d):
    dt = date.fromisoformat(d)
    return f"{dt.day} {dt.strftime('%b')}"


def compose(d, site_url):
    """(subject, text lines) for a normal day."""
    t, hist, a = d["today"], d["history"], d.get("advice", {})
    dec = d.get("decision") or {}
    lines = []
    if t:
        prev = hist[-2] if len(hist) > 1 else None
        change = ""
        if prev:
            diff = t["structural_annual_eur"] - prev["structural_annual_eur"]
            change = " (same as yesterday)" if abs(diff) < 0.5 else f" ({'+' if diff > 0 else ''}{eur(diff)} vs yesterday)"
        subject = f"Wolfheze energy {short(t['date'])}: {t['best_supplier']} {eur(t['structural_annual_eur'])}/year{change}"
    else:
        subject = f"Wolfheze energy {short(d['today_date'])}: no complete prices yet"

    pick = dec.get("pick")
    if pick:
        lines.append(f"Recommendation: {pick['supplier']} ({LABELS[pick['contract_type']]})")
        rng = "" if pick["low"] == pick["high"] else f" (range {eur(pick['low'])} – {eur(pick['high'])})"
        lines.append(f"  Expected first year from {short(dec['first_year_from'])}: {eur(pick['base'])}{rng}, "
                     f"{pick['confidence']} confidence.")
        for r in dec.get("reasons", []):
            lines.append(f"  {r}")
        for w in dec.get("watch", []):
            lines.append(f"  Watch: {w}")
        for dl in dec.get("deadlines", []):
            if 0 <= dl["days_left"] <= 45:
                lines.append(f"  Deadline: {dl['what']} by {short(dl['by'])} ({dl['days_left']} days).")
        lines.append("")

    if t:
        lines.append(f"Cheapest variable deal today: {t['best_supplier']} ({t['best_product']})")
        lines.append(f"  {eur(t['structural_annual_eur'])} a year supply: {per_kwh(t['kwh_price'])} per kWh"
                     f" + {eur(t['fixed_supply_eur_month'], 2)} a month{change}.")
        if t["avg30_eur"] is not None:
            diff = t["structural_annual_eur"] - t["avg30_eur"]
            pct = diff / t["avg30_eur"] * 100
            if abs(pct) < 0.05:
                lines.append(f"  Equal to the 30-day average ({eur(t['avg30_eur'])}).")
            else:
                lines.append(f"  {abs(pct):.1f}% {'cheaper' if diff < 0 else 'more expensive'} than the "
                             f"30-day average ({eur(t['avg30_eur'])}).")
        e = a.get("energy", {})
        if e.get("source_type") == "comparison":
            lines.append("  Note: this price is only from a comparison site so far.")
        ranked = [r for r in d["ranking"] if r["rankable"]][:3]
        if ranked:
            lines.append("  Top 3: " + "; ".join(
                f"{r['supplier']} {eur(r['structural_annual_eur'])}" + ("" if r.get("trusted", True) else " (unconfirmed)")
                for r in ranked) + ".")

    ann = d.get("announced") or []
    if ann:
        lines += ["", "Announced price changes:"]
        for x in ann[:5]:
            ch = f" ({x['change_pct']:+.0%})" if x.get("change_pct") is not None else ""
            lines.append(f"  {x['supplier']} from {short(x['valid_from'])}: {per_kwh(x['kwh_price'])} per kWh{ch}.")

    mkt = (d.get("market") or {}).get("power")
    if mkt and mkt.get("change_30d") is not None:
        lines += ["", f"Wholesale power: {per_kwh(mkt['avg_30d'])} per kWh (30-day average, excl. VAT), "
                      f"{mkt['change_30d']:+.0%} vs the previous 30 days, {mkt['change_vs_year_ago']:+.0%} vs a year ago."]

    i = a.get("internet")
    if i:
        avail = "available at your address" if i["available_at_address"] == "yes" else "availability not confirmed"
        lines += ["", f"Internet: {i['provider']} {i['product']}, {int(i['download_mbps'])} Mbit/s, "
                      f"{eur(i['first_year_eur'])} first year ({avail})."]
    sw = d.get("switch_alert")
    if sw:
        lines += ["", f"SWITCH ALERT: {sw['cheaper']} has been at least {eur(sw['saving_eur_year'])}/year cheaper than "
                      f"{sw['current']} for {sw['days']} days."]

    help_items = (d.get("help_wanted") or {}).get("items", [])
    if help_items and date.fromisoformat(d["today_date"]).weekday() == 0:
        lines += ["", f"Help wanted (Mondays): {len(help_items)} supplier(s) need a price copied from their website: "
                      + ", ".join(x["supplier"] for x in help_items) + ". See the dashboard."]
    ops = d.get("ops")
    if ops and date.fromisoformat(d["today_date"]).weekday() == 0:
        acc = ops.get("forecast_accuracy") or []
        acc_txt = "; ".join(f"{x['horizon_months']} mo ahead: {x['mape']:.1%} error (n={x['n']})" for x in acc) or "not scored yet"
        lines += ["", f"Weekly status: {ops['trusted_complete']}/{ops['suppliers_total']} suppliers complete and confirmed; "
                      f"oldest check {ops['max_days_since_check']} days; forecast accuracy: {acc_txt}."]

    warnings = d.get("health", {}).get("warnings", [])
    if warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in warnings]
    lines += ["", f"Dashboard: {site_url}"]
    return subject, lines


def to_html(lines, site_url):
    body = []
    for line in lines:
        if not line:
            body.append("<br>")
            continue
        esc = html.escape(line)
        if line.startswith("Dashboard: "):
            esc = f'<a href="{html.escape(site_url)}">Open the dashboard with all prices</a>'
        style = ("padding-left:16px;" if line.startswith("  ")
                 else "font-weight:600;" if line.endswith(":") or line.startswith("Recommendation") else "")
        body.append(f'<div style="{style}">{esc}</div>')
    return ('<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:15px;line-height:1.5;'
            'color:#0b0b0b;max-width:640px">' + "".join(body) + "</div>")


def alert(run_url, site_url):
    return ("Wolfheze energy: today's update failed",
            ["Today's dashboard update failed validation or a safety check, so the dashboard still shows the last good data.",
             "", f"Details: {run_url}", "", f"Dashboard: {site_url}"])


def brevo_request(api_key, sender, recipients, subject, lines, site_url):
    body = {
        "sender": {"name": "Wolfheze Energy", "email": sender},
        "to": [{"email": r} for r in recipients],
        "subject": subject,
        "textContent": "\n".join(lines),
        "htmlContent": to_html(lines, site_url),
    }
    return urllib.request.Request(BREVO_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "api-key": api_key, "content-type": "application/json", "accept": "application/json"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", required=True)
    ap.add_argument("--site-url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if os.environ.get("BUILD_OK", "true") != "true" or not Path(args.dashboard).exists():
        subject, lines = alert(os.environ.get("RUN_URL", "(no link)"), args.site_url)
    else:
        subject, lines = compose(json.loads(Path(args.dashboard).read_text(encoding="utf-8")), args.site_url)

    if args.dry_run:
        print(subject)
        print("\n".join(lines))
        return 0

    api_key, sender = os.environ.get("BREVO_API_KEY"), os.environ.get("EMAIL_FROM")
    to = [x.strip() for x in os.environ.get("EMAIL_TO", "").split(",") if x.strip()]
    if not (api_key and sender and to):
        print("Email not configured (set BREVO_API_KEY, EMAIL_FROM and EMAIL_TO); skipping.")
        return 0
    try:
        with urllib.request.urlopen(brevo_request(api_key, sender, to, subject, lines, args.site_url), timeout=30) as r:
            print(f"Sent '{subject}' to {len(to)} recipient(s): HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"Brevo refused the email: HTTP {e.code} {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
