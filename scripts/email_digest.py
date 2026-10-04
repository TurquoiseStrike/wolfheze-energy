"""Send the daily email digest built from dashboard.json. Standard library only.

  python scripts/email_digest.py --dashboard _site/dashboard.json --site-url https://... [--dry-run]

Environment (GitHub Actions secrets):
  GMAIL_USER, GMAIL_APP_PASSWORD   Gmail account + app password used to send
  EMAIL_TO                         comma-separated recipients
  BUILD_OK                         "true" unless today's build failed; then an alert is sent instead
  RUN_URL                          link to the Actions run, used in the alert
"""

import argparse
import html
import json
import os
import smtplib
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path


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
    lines = []
    if t:
        prev = hist[-2] if len(hist) > 1 else None
        change = ""
        if prev:
            diff = t["structural_annual_eur"] - prev["structural_annual_eur"]
            change = " (same as yesterday)" if abs(diff) < 0.5 else f" ({'+' if diff > 0 else ''}{eur(diff)} vs yesterday)"
        subject = f"Wolfheze energy {short(t['date'])}: {t['best_supplier']} {eur(t['structural_annual_eur'])}/year{change}"
        lines.append(f"Best variable electricity deal today: {t['best_supplier']} ({t['best_product']})")
        lines.append(f"  {eur(t['structural_annual_eur'])} a year supply: {per_kwh(t['kwh_price'])} per kWh"
                     f" + {eur(t['fixed_supply_eur_month'], 2)} a month{change}.")
        if t["avg30_eur"] is not None:
            diff = t["structural_annual_eur"] - t["avg30_eur"]
            pct = diff / t["avg30_eur"] * 100
            if abs(pct) < 0.05:
                lines.append(f"  Equal to the 30-day average ({eur(t['avg30_eur'])}).")
            else:
                word = "cheaper" if diff < 0 else "more expensive"
                lines.append(f"  {abs(pct):.1f}% {word} than the 30-day average ({eur(t['avg30_eur'])}).")
        e = a.get("energy", {})
        if e.get("source_type") == "comparison":
            lines.append("  Note: this price is only from a comparison site so far.")
        if e.get("consistent") and e["consistent"]["supplier"] != t["best_supplier"]:
            c = e["consistent"]
            lines.append(f"  Cheapest on average over the last {c['days']} days: {c['supplier']} ({eur(c['avg_annual_eur'])}/year).")
        ranked = [r for r in d["ranking"] if r["rankable"]][:3]
        if ranked:
            lines.append("")
            lines.append("Top 3 today:")
            for i, r in enumerate(ranked, 1):
                lines.append(f"  {i}. {r['supplier']}: {eur(r['structural_annual_eur'])}/year"
                             + ("" if r["source_type"] == "official" else " (comparison site)"))
    else:
        subject = f"Wolfheze energy {short(d['today_date'])}: no complete prices yet"
        lines.append("No supplier has a complete price (kWh price + fixed charge) yet.")

    i = a.get("internet")
    if i:
        avail = "available at your address" if i["available_at_address"] == "yes" else "availability not confirmed"
        lines += ["", f"Internet: {i['provider']} {i['product']}, {int(i['download_mbps'])} Mbit/s, "
                      f"{eur(i['first_year_eur'])} first year ({avail})."]
    s = a.get("solar")
    if s:
        lines.append(f"Solar panels later: {s['supplier']} lets you keep the most per kWh fed back "
                     f"({per_kwh(s['net_eur_kwh'])} net).")
    b = d.get("budget")
    if b:
        water = f" + water {eur(b['water_eur_year'])}" if b.get("water_eur_year") is not None else ""
        lines.append(f"Budget: electricity {eur(b['total_eur_year'])} a year all-in{water}.")

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
        indent = "padding-left:16px;" if line.startswith("  ") else "font-weight:600;" if line.endswith(":") else ""
        body.append(f'<div style="{indent}">{esc}</div>')
    return ('<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:15px;line-height:1.5;'
            'color:#0b0b0b;max-width:640px">' + "".join(body) + "</div>")


def alert(run_url, site_url):
    return ("Wolfheze energy: today's update failed",
            ["Today's dashboard update failed validation or a safety check, so the dashboard still shows the last good data.",
             "", f"Details: {run_url}", "", f"Dashboard: {site_url}"])


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

    user, password = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    to = [x.strip() for x in os.environ.get("EMAIL_TO", "").split(",") if x.strip()]
    if not (user and password and to):
        print("Email not sent: GMAIL_USER, GMAIL_APP_PASSWORD and EMAIL_TO must be set.", file=sys.stderr)
        return 1
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Wolfheze Energy <{user}>", ", ".join(to)
    msg.set_content("\n".join(lines))
    msg.add_alternative(to_html(lines, args.site_url), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Sent '{subject}' to {len(to)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
