"""Render the built dashboard in headless Chromium and fail on JavaScript errors or empty sections.

  python tests/smoke_site.py _site
Needs: pip install playwright && python -m playwright install --with-deps chromium
"""

import functools
import http.server
import json
import sys
import threading
from pathlib import Path


def required_sections(dash):
    """Sections that must show real content, given what's in the data."""
    req = ["ops", "help"]
    if dash.get("today"):
        req += ["decision", "advice", "best", "chart", "top"]
    if dash.get("market"):
        req.append("market")
    if dash.get("contracts", {}).get("options"):
        req.append("options")
    if dash.get("forecast", {}).get("variable"):
        req.append("forecast")
    return req


def main(site_dir):
    from playwright.sync_api import sync_playwright

    REQUIRED = required_sections(json.loads((Path(site_dir) / "dashboard.json").read_text(encoding="utf-8")))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site_dir)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, scheme in ((1200, "light"), (400, "dark")):
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
            page.on("pageerror", lambda e: problems.append(f"[{width}px] JS error: {e}"))
            page.on("console", lambda m: m.type == "error" and problems.append(f"[{width}px] console: {m.text}"))
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(500)
            for section in REQUIRED:
                el = page.locator(f"#{section}")
                if not el.count():
                    problems.append(f"[{width}px] missing section #{section}")
                elif el.locator(".empty").count():
                    problems.append(f"[{width}px] section #{section} shows its empty state")
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            if overflow:
                problems.append(f"[{width}px] page scrolls horizontally")
        browser.close()
    server.shutdown()
    for p_ in problems:
        print(p_)
    print("smoke test: " + ("FAILED" if problems else "ok"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_site"))
