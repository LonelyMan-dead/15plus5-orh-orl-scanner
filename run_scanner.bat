@echo off
echo ============================================================
echo  UNIFIED 15+5 SCANNER — Long + Short
echo  Reads: gen_scanner\config\market_candidates.txt
echo  Format: L:TICKER (long), S:TICKER (short), TICKER (both)
echo ============================================================
echo.
cd /d "%~dp0"
python scanner.py
pause
