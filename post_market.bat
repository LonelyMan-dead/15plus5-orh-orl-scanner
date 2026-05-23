@echo off
echo ============================================================
echo  POST-MARKET DAILY REVIEW + DASHBOARD UPDATE
echo  Analyzes today's scans, prints review, opens dashboard
echo ============================================================
echo.
cd /d "%~dp0"
python post_market.py
python generate_dashboard.py
echo.
echo Opening dashboard in browser...
start "" "dashboard\daily_dashboard.html"
pause
