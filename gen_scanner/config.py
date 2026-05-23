"""
═══════════════════════════════════════════════════════════════════════════════
UNIFIED 15+5 SCANNER  —  Config v1.0
Covers BOTH strategies from a single IBKR connection.
  Long  : ORH Break → Retest → Continue Higher
  Short : ORL Break → Retest → Continue Lower

Ticker format in market_candidates.txt
  L:TICKER   →  evaluated for long only
  S:TICKER   →  evaluated for short only
  TICKER     →  evaluated for BOTH (scanner picks best direction)
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT_DIR              = Path(__file__).resolve().parents[1]   # FIX: was parents[2] — off by one level
GEN_DIR               = ROOT_DIR / "gen_scanner"
OUTPUT_DIR            = ROOT_DIR / "output"
REPORTS_DIR           = ROOT_DIR / "reports"
LEARNING_DIR          = ROOT_DIR / "learning"
CONFIG_DIR            = GEN_DIR  / "config"
MARKET_CANDIDATES_PATH = CONFIG_DIR / "market_candidates.txt"

for _p in (OUTPUT_DIR, REPORTS_DIR, LEARNING_DIR, CONFIG_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# ── IBKR ───────────────────────────────────────────────────────────────────
IB_HOST      = "127.0.0.1"
IB_PORT      = 7496          # 7497 = paper, 4001 = Gateway live, 4002 = Gateway paper
IB_CLIENT_ID = 1             # change if another app owns id=1

# ── Feed mode ──────────────────────────────────────────────────────────────
# FEEDER_ENABLED = False → MANUAL mode (reads market_candidates.txt)
# FEEDER_ENABLED = True  → AUTO mode (IBKR discovers tickers — requires TWS)
FEEDER_ENABLED = False

# ── Scan cadence ───────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS   = 60
FEEDER_REFRESH_SECONDS  = 180

# ── Universe guardrails (IBKR pre-filter aligned) ──────────────────────────
PRICE_MIN        = 10.0
PRICE_MAX        = 50.0
AVG_VOLUME_MIN   = 1_000_000
SPREAD_MAX_PCT   = 0.006       # 0.6% max avg spread
RVOL_PREF_MIN    = 1.50        # preferred min RVOL for TRIGGERED state

# ── Risk / position sizing ──────────────────────────────────────────────────
ACCOUNT_SIZE              = 50_000.0   # ← Set your account value
ACCOUNT_RISK_PCT          = 0.005      # 0.5% risk per trade (spec Step 5A)
MAX_POSITION_EXPOSURE_PCT = 0.30       # hard cap: no single name > 30% account

# ── RR thresholds ──────────────────────────────────────────────────────────
RR_MIN                = 1.5   # hard gate for TRIGGERED / TRADE READY
WATCHLIST_RR_SOFT_MIN = 1.2   # soft advisory for WATCHLIST visibility

# ── Grade thresholds (both directions share same grade boundaries) ──────────
GRADE_A_PLUS_MIN = 88   # ≥ 88 raw → A+
GRADE_A_MIN      = 77   # ≥ 77      → A
GRADE_B_MIN      = 63   # ≥ 63      → B  (watchlist)
                         # < 63      → Skip

# ── A+ scarcity cap ─────────────────────────────────────────────────────────
A_PLUS_ACTIVE_CAP  = 1   # only 1 active A+ at a time (combined long + short)
A_PLUS_SESSION_CAP = 2   # max 2 A+ per session

# ── Indicators ─────────────────────────────────────────────────────────────
EMA_FAST           = 8
EMA_SLOW           = 20
SUPERTREND_PERIOD  = 10
SUPERTREND_MULT    = 3.0
DONCHIAN_5M        = 10   # 5m chart
DONCHIAN_15M       = 20   # 15m chart
ATR_PERIOD         = 14

# ── Stop construction ───────────────────────────────────────────────────────
ATR_STOP_BUFFER_MULT   = 0.10   # stop buffer = 0.1 × ATR(5m)
PCT_STOP_BUFFER        = 0.0008 # or 0.08% of price (whichever is larger)
MIN_STOP_NOISE_PCT     = 0.0035 # minimum stop distance as % of price
MIN_STOP_ATR_MULT      = 0.35   # minimum stop distance as multiple of ATR
MIN_STOP_RANGE_MULT    = 0.60   # minimum stop relative to avg candle range

# ── Structure rules ─────────────────────────────────────────────────────────
VWAP_FLAT_THRESHOLD_PCT = 0.0008  # VWAP slope < this → treated as flat
EMA_SEP_LOOKBACK        = 4       # bars to check EMA active separation
NEAR_LOW_MAX_PCT        = 0.30    # 15m candle: close must be bottom 30%
NEAR_HIGH_MAX_PCT       = 0.70    # 15m candle (long): close must be top 30% (= 1 - 0.30)
LOWER_WICK_MAX_PCT      = 0.40    # short: lower wick ≤ 40% of candle range
UPPER_WICK_MAX_PCT      = 0.40    # long:  upper wick ≤ 40% of candle range
TWO_TAP_TOLERANCE_PCT   = 0.003

# ── Volume ──────────────────────────────────────────────────────────────────
VOL_AVG_WINDOW               = 10   # 10-bar (50-min) volume average
BREAKDOWN_VOLUME_MULT        = 1.20  # breakdown/breakout bar volume ≥ 1.2× avg
BEAR_FLAG_MIN_CANDLES        = 3
BEAR_FLAG_MAX_CANDLES        = 6
BEAR_FLAG_MAX_RANGE_PCT      = 0.018 # tight base < 1.8% of price
BULL_FLAG_MIN_CANDLES        = 3
BULL_FLAG_MAX_CANDLES        = 6
BULL_FLAG_MAX_RANGE_PCT      = 0.018

# ── Confirmers / trap thresholds ───────────────────────────────────────────
CONFIRMER_REQUIRED_COUNT = 2   # need ≥ 2 of 4 confirmers
TRAP_BLOCK_COUNT         = 2   # ≥ 2 liquidity trap conditions → no trade

# ── Final execution engine ─────────────────────────────────────────────────
FINAL_ENGINE_RECENT_BARS            = 4
FINAL_ENGINE_MATURE_BARS            = 10
FINAL_ENGINE_MAX_SIDEWAYS_BARS      = 6
FINAL_ENGINE_BREAKDOWN_LOOKBACK     = 18
FINAL_ENGINE_COMPRESSION_ATR_MULT   = 0.60
FINAL_ENGINE_COMPRESSION_PCT        = 0.0028
FINAL_ENGINE_MIN_FRESH_ATR_MULT     = 0.18
FINAL_ENGINE_MIN_FRESH_PCT          = 0.0010
FINAL_ENGINE_MIN_BODY_ATR_MULT      = 0.34
FINAL_ENGINE_MIN_BODY_PCT           = 0.0018
FINAL_ENGINE_MAX_OR_TRAVEL_ATR_LATE = 2.45
FINAL_ENGINE_MAX_VWAP_TRAVEL_ATR_LATE = 2.05
FINAL_ENGINE_MAX_TARGET_ATR_LATE    = 1.45
FINAL_ENGINE_MIN_TARGET_ATR         = 0.38
FINAL_ENGINE_MIN_VOL_RATIO_LATE     = 0.65
FINAL_ENGINE_LATE_TIME              = "11:30"

# ── Near-support / near-resistance proximity ────────────────────────────────
NEAR_SUPPORT_PCT     = 0.006   # short: T1 must be ≥ 0.6% below ORL
NEAR_RESISTANCE_PCT  = 0.006   # long:  T1 must be ≥ 0.6% above ORH

# ── Time-window session bonus ───────────────────────────────────────────────
SESSION_WINDOW_BONUS = 5   # extra raw score points when in primary window

# ── Top N focus list ────────────────────────────────────────────────────────
TOP_FOCUS_COUNT = 6   # show top 6 across both directions combined

# ── Gap quality floor ────────────────────────────────────────────────────────
GAP_QUALITY_PCT = 0.005  # ≥ 0.5% gap (either direction) is a positive signal

# ── Learning engine ─────────────────────────────────────────────────────────
LEARNING_MIN_SAMPLE   = 5
LEARNING_RECENCY_DAYS = 45

TIMEZONE = "America/New_York"
