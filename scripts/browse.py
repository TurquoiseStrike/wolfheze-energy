"""Open a page in a real (headless) browser and print what a person would see.

For pages that only show prices after JavaScript runs or after you fill in a form
(postcode checks, tariff calculators). One-time setup per run: bash scripts/setup_browser.sh

  python3 scripts/browse.py URL
  python3 scripts/browse.py URL --grep "vaste leveringskosten|per maand"
  python3 scripts/browse.py URL --links "\\.pdf"
  python3 scripts/browse.py URL --step "fill:input[name=postcode]=6874AB" --step "fill:#huisnummer=1" \\
      --step "click:text=Bekijk tarieven" --step "wait:3" --grep "kWh"

Steps run in order:  fill:SELECTOR=VALUE | click:SELECTOR | press:SELECTOR=KEY | wait:SECONDS | select:SELECTOR=VALUE
SELECTOR is any Playwright selector: CSS, "text=Volgende", "role=button[name='Bekijk']", ...
Cookie banners are dismissed automatically where a common accept button exists.
"""

import argparse
import re
import sys

COOKIE_BUTTONS = [
    "Alles accepteren", "Accepteren", "Akkoord", "Alle cookies accepteren", "Accept all", "Accept",
    "Ja, ik ga akkoord", "Toestaan", "Alles toestaan", "Cookies accepteren", "OK",
]


def dismiss_cookies(page):
    for label in COOKIE_BUTTONS:
        try:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count():
                button.first.click(timeout=1500)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def run_step(page, step):
    kind, _, arg = step.partition(":")
    if kind == "wait":
        page.wait_for_timeout(float(arg) * 1000)
        return
    selector, _, value = arg.partition("=") if kind in ("fill", "press", "select") else (arg, "", "")
    target = page.locator(selector).first
    if kind == "fill":
        target.fill(value, timeout=10000)
    elif kind == "click":
        target.click(timeout=10000)
    elif kind == "press":
        target.press(value, timeout=10000)
    elif kind == "select":
        target.select_option(value, timeout=10000)
    else:
        raise SystemExit(f"unknown step {step!r}")
    page.wait_for_timeout(800)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--step", action="append", default=[], help="fill:SEL=VAL, click:SEL, press:SEL=KEY, wait:SEC, select:SEL=VAL")
    ap.add_argument("--grep", help="only print lines matching this regex (case-insensitive), with one line of context")
    ap.add_argument("--links", help="print links whose URL or text matches this regex")
    ap.add_argument("--screenshot", help="save a full-page screenshot to this path")
    ap.add_argument("--max-chars", type=int, default=20000)
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="nl-NL", user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        dismiss_cookies(page)
        for step in args.step:
            run_step(page, step)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        print(f"URL: {page.url}")
        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
        if args.links:
            rx = re.compile(args.links, re.IGNORECASE)
            for a in page.locator("a[href]").all():
                href, text = a.get_attribute("href") or "", (a.inner_text() or "").strip()
                if rx.search(href) or rx.search(text):
                    print(f"LINK {text[:80]!r} -> {page.url if href.startswith('#') else href}")
        text = page.locator("body").inner_text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if args.grep:
            rx = re.compile(args.grep, re.IGNORECASE)
            keep = set()
            for i, ln in enumerate(lines):
                if rx.search(ln):
                    keep.update({i - 1, i, i + 1})
            lines = [lines[i] for i in sorted(keep) if 0 <= i < len(lines)]
        out = "\n".join(lines)
        print(out[: args.max_chars] + ("\n[... truncated]" if len(out) > args.max_chars else ""))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
