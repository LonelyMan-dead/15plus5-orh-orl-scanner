"""Data feed — yfinance manual mode (FEEDER_ENABLED = False).

Fetches 5m and 15m bars from Yahoo Finance for symbols listed in
market_candidates.txt. No IBKR connection required in this mode.

Ticker format in market_candidates.txt:
  L:AAPL   → long candidate only
  S:MSFT   → short candidate only
  TSLA     → both directions evaluated (scanner picks best)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import MARKET_CANDIDATES_PATH, PRICE_MIN, PRICE_MAX, SPREAD_MAX_PCT, TIMEZONE

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False


# ─── Candidate record ─────────────────────────────────────────────────────

@dataclass
class CandidateTicker:
    symbol: str
    direction: str   # "long", "short", or "both"


@dataclass
class MarketDataBundle:
    symbol: str
    direction: str
    bars_5m:  pd.DataFrame
    bars_15m: pd.DataFrame
    avg_volume: int
    avg_spread_pct: float
    prev_high: float
    prev_low: float
    prev_close: float


# ─── Market candidates reader ─────────────────────────────────────────────

def load_candidates(path: Path = MARKET_CANDIDATES_PATH) -> list[CandidateTicker]:
    """Read market_candidates.txt, returning CandidateTicker objects.

    Format:
      L:TICKER   →  long only
      S:TICKER   →  short only
      TICKER     →  both directions
      # comment  →  ignored
    """
    candidates: list[CandidateTicker] = []
    seen: set[str] = set()

    if not path.exists():
        print(f"[FEED] market_candidates.txt not found at {path}")
        print("[FEED] Create it and add tickers: L:AAPL, S:MSFT, or just TSLA")
        return candidates

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line_upper = line.upper()
        if line_upper.startswith("L:"):
            sym = line_upper[2:].strip()
            direction = "long"
        elif line_upper.startswith("S:"):
            sym = line_upper[2:].strip()
            direction = "short"
        else:
            sym = line_upper.strip()
            direction = "both"

        if not sym or sym in seen:
            continue
        seen.add(sym)
        candidates.append(CandidateTicker(symbol=sym, direction=direction))

    return candidates


# ─── yfinance fetch ───────────────────────────────────────────────────────

def _fetch_yf(symbol: str, interval: str, period: str,
              retries: int = 2) -> pd.DataFrame:
    """Fetch bars from yfinance with retry. Returns empty df on failure."""
    if not _YF_AVAILABLE:
        print("[FEED] yfinance not installed — run: pip install yfinance")
        return pd.DataFrame()

    for attempt in range(retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(interval=interval, period=period,
                                prepost=False, auto_adjust=True)
            if df.empty:
                return pd.DataFrame()
            df.columns = [c.lower() for c in df.columns]
            # Ensure DatetimeIndex with timezone
            if isinstance(df.index, pd.DatetimeIndex):
                if df.index.tzinfo is None:
                    df.index = df.index.tz_localize("UTC").tz_convert(TIMEZONE)
                else:
                    df.index = df.index.tz_convert(TIMEZONE)
            # Filter to RTH only
            rth = (df.index.hour > 9) | ((df.index.hour == 9) & (df.index.minute >= 30))
            rth &= df.index.hour < 16
            df = df[rth]
            return df
        except Exception as exc:
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"[FEED] {symbol} {interval}: {exc}")
    return pd.DataFrame()


def _estimate_spread(df5: pd.DataFrame) -> float:
    """Estimate avg spread as a fraction of price from recent OHLC bars."""
    try:
        recent = df5.tail(10)
        if recent.empty:
            return 0.0
        spreads = (recent["high"] - recent["low"]) / recent["close"].replace(0, float("nan"))
        return float(spreads.median()) * 0.15   # rough proxy: spread ≈ 15% of bar range
    except Exception:
        return 0.0


def _prior_day_ohlc(df5: pd.DataFrame) -> tuple[float, float, float]:
    """Extract prior-day high, low, close from multi-day 5m bar data."""
    try:
        if isinstance(df5.index, pd.DatetimeIndex):
            dates = sorted(set(df5.index.date))
            if len(dates) >= 2:
                prior_date = dates[-2]
                prior = df5[df5.index.date == prior_date]
                if not prior.empty:
                    return (float(prior["high"].max()),
                            float(prior["low"].min()),
                            float(prior["close"].iloc[-1]))
    except Exception:
        pass
    return 0.0, 0.0, 0.0


# ─── Main data loader ─────────────────────────────────────────────────────

def load_market_data(candidate: CandidateTicker) -> Optional[MarketDataBundle]:
    """Fetch all required bars for one candidate symbol.

    Returns None when the symbol fails price/volume/data checks.
    """
    sym = candidate.symbol

    # 5m bars: 5-day window (gives ~2 full trading days of RTH 5m bars)
    bars_5m = _fetch_yf(sym, interval="5m", period="5d")
    if bars_5m.empty or len(bars_5m) < 12:
        print(f"[FEED] {sym}: insufficient 5m bars ({len(bars_5m)}), skipping")
        return None

    # Price universe check
    last_price = float(bars_5m["close"].iloc[-1])
    if not (PRICE_MIN <= last_price <= PRICE_MAX):
        print(f"[FEED] {sym}: price ${last_price:.2f} outside ${PRICE_MIN:.0f}–${PRICE_MAX:.0f}, skipping")
        return None

    # 15m bars: same 5-day window
    bars_15m = _fetch_yf(sym, interval="15m", period="5d")
    if bars_15m.empty or len(bars_15m) < 4:
        print(f"[FEED] {sym}: insufficient 15m bars, skipping")
        return None

    # Volume check (use 20-bar avg of 5m to get daily equivalent)
    avg_volume = int(bars_5m["volume"].tail(78).sum())   # approx 1 full day
    if avg_volume < 200_000:
        print(f"[FEED] {sym}: volume {avg_volume:,} too low, skipping")
        return None

    # Spread proxy
    avg_spread = _estimate_spread(bars_5m)
    if avg_spread > SPREAD_MAX_PCT * 2:   # generous gate: spread check is indicative only with yf data
        print(f"[FEED] {sym}: estimated spread {avg_spread*100:.2f}% too wide, skipping")
        return None

    prev_high, prev_low, prev_close = _prior_day_ohlc(bars_5m)

    return MarketDataBundle(
        symbol=sym,
        direction=candidate.direction,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        avg_volume=avg_volume,
        avg_spread_pct=avg_spread,
        prev_high=prev_high,
        prev_low=prev_low,
        prev_close=prev_close,
    )
