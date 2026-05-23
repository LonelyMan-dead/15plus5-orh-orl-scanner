# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A real-time intraday stock scanner implementing the **15+5 strategy**: it scans for Opening Range High (ORH) breakout **LONG** and Opening Range Low (ORL) breakdown **SHORT** setups on US equities during RTH (9:30–16:00 ET). The scanner runs continuously, aligned to 5-minute candle closes, and evaluates each candidate through a locked 5-stage pipeline before assigning an actionable state.

## Running the scanner

```powershell
# Live scan (runs continuously, Ctrl+C to stop)
python scanner.py

# Post-market daily review (after close)
python post_market.py

# Log trade outcomes interactively
python log_outcome.py

# Generate HTML performance dashboard
python generate_dashboard.py
```

**Feed mode** is controlled by `FEEDER_ENABLED` in `gen_scanner/config.py`:
- `False` — yfinance mode (no TWS needed; Yahoo data; default for testing)
- `True`  — IBKR live mode (TWS must be running on `IB_HOST:IB_PORT`)

## Candidate list

Edit `gen_scanner/config/market_candidates.txt` before each session. The scanner re-reads it every cycle — no restart needed.

```
L:AAPL   # long only
S:MSFT   # short only
TSLA     # both directions
```

Keep the combined list to ≤ 20 tickers to stay within the 60s scan cadence.

## Evaluation pipeline (locked order — do not reorder)

Each `(symbol, direction)` pair flows through five stages in `scanner._evaluate()`:

| Stage | Module | What it does |
|-------|--------|--------------|
| **A** | `structure_validation.validate_structure()` | Hard gates: price universe, VWAP/EMA direction, OR break, Supertrend, confirmers (≥2/4 required), traps (<2 allowed), 10:30+ strict mode |
| **B** | `quality_scoring.score_quality()` | 15-component score (max raw ~130, clamped 0–100) → grade A+/A/B/Skip |
| **C** | `state_machine.assign_state()` | Preliminary state based on TYPE_A or TYPE_B trigger conditions |
| **D** | `risk_targets.calculate_risk_targets()` | Survivable stop (ranked hierarchy), T1/T2 from pivot walls, R:R, position size |
| **E** | `state_machine.evaluate_final_eligibility()` | Final execution engine: time gate, fresh impulse, compression, late exhaustion, T1 realism |

After E, `scanner.scan_once()` applies three **priority overrides** in order:
1. **Manual pause** (`TRADING_PAUSED.txt` present) → caps TRIGGERED to WATCHLIST
2. **Daily cap** (`DAILY_MAX_TRIGGERED` reached) → same cap
3. **Market regime** (SPY/QQQ assessed once per cycle via `market_regime.fetch_and_assess()`) → suppresses counter-trend direction or downgrades grades in CHOPPY

Then **macro calendar** (FOMC/CPI/NFP ±30 min) and **per-symbol earnings** gates cap TRIGGERED to WATCHLIST.

## State labels

- `TRIGGERED` — all gates clear, entry condition fired → execute
- `READY_CONTINUATION` — setup valid, awaiting final trigger
- `WATCHLIST` — structurally valid, not ready yet
- `AVOID` — hard gate failure, skip

## Entry types (TYPE_A vs TYPE_B)

Both directions support two entry patterns, classified in `structure_validation` and used throughout:

- **TYPE_A** — OR level retest → rejection → new low/high with volume (primary)
- **TYPE_B** — tight flag base below/above OR level (≤1.8% range, 3+ bars) → flag boundary break with volume

State machine trigger logic is separate for each type — they fire on different conditions.

## Key configuration (`gen_scanner/config.py`)

Most tuning happens here. Important constants:
- `PRICE_MIN/MAX` (10–50), `AVG_VOLUME_MIN` (1M), `SPREAD_MAX_PCT` (0.6%)
- `ACCOUNT_SIZE`, `ACCOUNT_RISK_PCT` (0.5%), `MAX_POSITION_EXPOSURE_PCT` (30%)
- `GRADE_A_PLUS_MIN=88`, `GRADE_A_MIN=77`, `GRADE_B_MIN=63`
- `RR_MIN=1.5` — hard gate; setups below this R:R are blocked
- `DAILY_MAX_TRIGGERED=4` — set to 0 to disable
- `SHORT_EXHAUSTED_GAP_PCT=0.05` — B11 trap threshold for big gap-down shorts
- `EVENT_BUFFER_MINUTES=30`, `EARNINGS_BUFFER_DAYS=1`

## Module map

```
scanner.py              — main loop: orchestrates pipeline, applies priority overrides, outputs
post_market.py          — daily review: reads CSVs, prints terminal report, updates daily_stats.csv
log_outcome.py          — interactive trade outcome entry
generate_dashboard.py   — builds dashboard/daily_dashboard.html from learning/ CSVs

gen_scanner/
  config.py             — all tuneable constants + path definitions
  data_feed.py          — yfinance fetch (FEEDER_ENABLED=False)
  data_ibkr.py          — IBKR live fetch (FEEDER_ENABLED=True)
  indicators.py         — add_indicators(): EMA8/20, ATR14, session VWAP, Supertrend, Donchian, vol_avg, SMA200
  structure_validation.py — Stage A: hard gates, confirmers, traps, entry type classification
  quality_scoring.py    — Stage B: 15-component score → grade
  state_machine.py      — Stages C+E: assign_state(), evaluate_final_eligibility(), time windows
  risk_targets.py       — Stage D: stop hierarchy, T1/T2 pivot walls, position sizing
  market_regime.py      — Priority 1: SPY/QQQ regime -> BULL_TREND/BEAR_TREND/CHOPPY/UNKNOWN
  event_calendar.py     — Priority 2/3: FOMC/CPI/NFP blackout, per-symbol earnings gate
  reporter.py           — terminal output (CandidateRow, report(), write_csv(), write_trace())
  learning_logger.py    — CSV logging: setups_log, daily_stats, outcomes_log, performance_log
  utils.py              — safe_float, slope, last_bar_time, compute_pivots, gap_up/down_pct
```

## Output files

| Path | Content |
|------|---------|
| `output/unified_YYYYMMDD_results.csv` | All rows from every cycle that day |
| `reports/trace_YYYYMMDD_HHMMSS.txt` | Per-cycle decision trace |
| `learning/setups_log.csv` | Every evaluated setup (feeds post-market + dashboard) |
| `learning/daily_stats.csv` | Per-day aggregate stats |
| `learning/outcomes_log.csv` | User-entered trade results |
| `learning/performance_log.csv` | Running win-rate / net-R summary |
| `dashboard/daily_dashboard.html` | Standalone HTML dashboard |

## Indicators computed by `add_indicators()`

EMA8, EMA20, ATR14, session-reset VWAP (daily anchor at 9:30 ET), Supertrend (period=10, mult=3.0), Donchian upper/lower (window configurable — 10 on 5m, 20 on 15m), vol_avg (10-bar rolling), SMA200 (min_periods=200; NaN until 200 bars available). Indicators are computed independently for the 5m and 15m DataFrames — the Donchian window differs between them per spec.

## Pivot walls used for T1/T2 and trap proximity

`utils.compute_pivots()` returns: PP, R1, R2, S1, S2, S3, ROUND_DN1/DN2, ROUND_UP1/UP2, PDH, PDL, SMA200. Round-number levels are treated as first-class walls — proximity to them (0.4%) triggers traps B8/L8.

## Dependencies

`pandas`, `numpy`, `yfinance` (always required even in IBKR mode — used for earnings calendar and regime fallback). IBKR mode additionally requires `ib_insync` or equivalent (see `data_ibkr.py`). All timezone handling uses Python's built-in `zoneinfo` (Python >= 3.9).
