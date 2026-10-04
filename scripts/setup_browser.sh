#!/usr/bin/env bash
# Install a headless Chromium for scripts/browse.py (about 1-2 minutes, once per run).
set -euo pipefail
python3 -m pip install --quiet playwright
python3 -m playwright install --with-deps chromium >/dev/null
python3 -c "from playwright.sync_api import sync_playwright; print('browser ready')"
