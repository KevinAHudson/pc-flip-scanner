@echo off
REM Windows: double-click or schedule with Task Scheduler. First run installs deps.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q playwright==1.48.0
  .venv\Scripts\python -m playwright install chromium
)
git pull -q 2>nul
.venv\Scripts\python -u scanner.py >> scan.log 2>&1
if "%1"=="" start "" report.html
