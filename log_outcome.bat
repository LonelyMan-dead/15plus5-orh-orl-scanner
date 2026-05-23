@echo off
echo ============================================================
echo  TRADE OUTCOME LOGGER
echo  Log today's trade results into outcomes_log.csv
echo ============================================================
echo.
cd /d "%~dp0"
python log_outcome.py
pause
