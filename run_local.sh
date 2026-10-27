#!/usr/bin/env bash
# Linux/macOS: ./run_local.sh   (add --quiet for cron so it doesn't try to open the report)
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q playwright==1.48.0
  .venv/bin/python -m playwright install --with-deps chromium 2>/dev/null || .venv/bin/python -m playwright install chromium
fi
git pull -q 2>/dev/null
.venv/bin/python -u scanner.py 2>&1 | tee -a scan.log
[ "$1" != "--quiet" ] && (xdg-open report.html 2>/dev/null || open report.html 2>/dev/null) || true
