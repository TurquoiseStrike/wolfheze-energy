#!/usr/bin/env bash
# Install a headless Chromium for scripts/browse.py (about 1-2 minutes, once per run).
set -euo pipefail
python3 -m pip install --quiet playwright
python3 -m playwright install --with-deps chromium >/dev/null

# Cloud sessions route HTTPS through a proxy that re-terminates TLS. Chromium uses its own
# certificate store (NSS), so add the proxy's CA there; certificate checking stays on.
CA=/root/.ccr/agent-proxy-ca.crt
if [ -f "$CA" ]; then
  command -v certutil >/dev/null || apt-get install -y -q libnss3-tools >/dev/null
  mkdir -p "$HOME/.pki/nssdb"
  [ -f "$HOME/.pki/nssdb/cert9.db" ] || certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password
  certutil -d "sql:$HOME/.pki/nssdb" -A -t "CT,," -n agent-proxy -i "$CA"
fi
python3 -c "from playwright.sync_api import sync_playwright; print('browser ready')"
