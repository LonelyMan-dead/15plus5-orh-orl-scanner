@echo off
echo Generating HTML dashboard...
cd /d "%~dp0"
python generate_dashboard.py
echo Opening dashboard...
start "" "dashboard\daily_dashboard.html"
pause
