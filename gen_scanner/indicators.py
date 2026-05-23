"""Indicators — unified scanner v1.0.

Shared indicator calculations used by both the LONG and SHORT evaluation paths.
All indicators are computed once per symbol per cycle; the direction-specific
logic sits in structure_validation.py, quality_scoring.py, etc.
"""
from __future__ import annotations

import pandas as pd
import numpy as np
from .config import (
    EMA_FAST, EMA_SLOW, SUPERTREND_PERIOD, SUPERTREND_MULT,
    DONCHIAN_5M, DONCHIAN_15M, ATR_PERIOD, VOL_AVG_WINDOW, TIMEZONE
)
from .utils import safe_float


# ─── Building blocks ───────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, window: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window, min_periods=1).mean()


def _donchian(df: pd.DataFrame, window: int) -> pd.DataFrame:
    return pd.DataFrame({
        "donchian_upper": df["high"].rolling(window, min_periods=1).max(),
        "donchian_lower": df["low"].rolling(window, min_periods=1).min(),
        "donchian_mid":  (
            df["high"].rolling(window, min_periods=1).max()
            + df["low"].rolling(window, min_periods=1).min()
        ) / 2.0,
    }, index=df.index)


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session-reset VWAP anchored to 09:30 ET every day."""
    def _compute_group(group: pd.DataFrame) -> pd.Series:
        price  = (group["high"] + group["low"] + group["close"]) / 3.0
        volume = group["volume"].clip(lower=0)
        cum_vol = volume.cumsum()
        denom  = cum_vol.replace(0, np.nan)
        return (price * volume).cumsum() / denom

    try:
        if isinstance(df.index, pd.DatetimeIndex):
            idx = df.index
            if idx.tzinfo is None:
                idx = idx.tz_localize("UTC").tz_convert(TIMEZONE)
            else:
                idx = idx.tz_convert(TIMEZONE)
            # Filter to RTH (9:30+) within each trading day
            rth = (idx.hour > 9) | ((idx.hour == 9) & (idx.minute >= 30))
            session_df = df.loc[rth].copy() if rth.any() else df.copy()
            session_df.index = idx[rth] if rth.any() else idx
            date_keys = session_df.index.date
            groups = []
            for d in sorted(set(date_keys)):
                day_df = session_df[date_keys == d]
                groups.append(_compute_group(day_df))
            if groups:
                vwap_series = pd.concat(groups)
                return vwap_series.reindex(df.index).ffill().bfill()
    except Exception:
        pass
    # Fallback: session-level VWAP without date grouping
    price  = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].clip(lower=0)
    denom  = volume.cumsum().replace(0, np.nan)
    return (price * volume).cumsum() / denom


def _supertrend(df: pd.DataFrame,
                period: int = SUPERTREND_PERIOD,
                multiplier: float = SUPERTREND_MULT) -> pd.Series:
    """Returns bool Series: True = bullish (green), False = bearish (red = short OK)."""
    n = len(df)
    if n < period + 2:
        return pd.Series(False, index=df.index, dtype=bool)
    atr_vals  = _atr(df, period)
    hl2       = (df["high"] + df["low"]) / 2.0
    upper_raw = (hl2 + multiplier * atr_vals).astype(float).tolist()
    lower_raw = (hl2 - multiplier * atr_vals).astype(float).tolist()
    closes    = df["close"].astype(float).tolist()
    fu = upper_raw[:]
    fl = lower_raw[:]
    dirs: list[bool] = [True] * n
    for i in range(1, n):
        fu[i] = upper_raw[i] if (upper_raw[i] < fu[i-1] or closes[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lower_raw[i] if (lower_raw[i] > fl[i-1] or closes[i-1] < fl[i-1]) else fl[i-1]
        dirs[i] = (closes[i] >= fl[i]) if dirs[i-1] else (closes[i] > fu[i])
    return pd.Series(dirs, index=df.index, dtype=bool)


def _vol_avg(df: pd.DataFrame, window: int = VOL_AVG_WINDOW) -> pd.Series:
    return df["volume"].rolling(window, min_periods=1).mean()


# ─── Master add_indicators ─────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame, donchian_window: int = DONCHIAN_5M) -> pd.DataFrame:
    """Add all technical indicators to a bar DataFrame in-place (returns copy)."""
    if df.empty:
        return df.copy()
    out = df.copy()
    for col in ("high", "low", "open", "close", "volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out["ema8"]  = _ema(out["close"], EMA_FAST)
    out["ema20"] = _ema(out["close"], EMA_SLOW)
    out["atr14"] = _atr(out)
    out["vwap"]  = _session_vwap(out)
    out["supertrend_bullish"] = _supertrend(out)

    dc = _donchian(out, donchian_window)
    out["donchian_upper"] = dc["donchian_upper"]
    out["donchian_lower"] = dc["donchian_lower"]
    out["donchian_mid"]   = dc["donchian_mid"]

    out["vol_avg"]    = _vol_avg(out)
    # Relative volume: current bar vs rolling average
    out["rvol"]       = out["volume"] / out["vol_avg"].replace(0, np.nan)
    # Body ratio
    bar_range         = (out["high"] - out["low"]).replace(0, np.nan)
    out["body_ratio"] = (out["close"] - out["open"]).abs() / bar_range
    # Close position within bar (0 = at low, 1 = at high)
    out["close_pos"]  = (out["close"] - out["low"]) / bar_range
    # QC-5: 200-bar SMA of close — spec-required key wall level.
    # With a 5-day yfinance fetch (~390 RTH 5m bars) this is reliably computable.
    # min_periods=200 returns NaN until 200 bars are available — prevents false values.
    out["sma200"]     = out["close"].rolling(200, min_periods=200).mean()

    return out


# ─── Opening range helper ─────────────────────────────────────────────────

def opening_range(df15: pd.DataFrame) -> tuple[float, float]:
    """Return (ORH, ORL) from the first 15m candle of today."""
    if df15.empty:
        raise ValueError("15m bars are empty — cannot compute OR")
    try:
        if isinstance(df15.index, pd.DatetimeIndex):
            idx = df15.index
            if idx.tzinfo is None:
                idx = idx.tz_localize("UTC").tz_convert(TIMEZONE)
            else:
                idx = idx.tz_convert(TIMEZONE)
            today = idx.date.max()
            today_bars = df15[idx.date == today]
            if not today_bars.empty:
                first = today_bars.iloc[0]
                return float(first["high"]), float(first["low"])
    except Exception:
        pass
    first = df15.iloc[0]
    return float(first["high"]), float(first["low"])


# NOTE (QC-2): vwap_slope_score() was removed in v6 — confirmed dead code.
# The function was never called from any pipeline module.  Slope-based VWAP
# assessment is handled by the _slope() utility used in structure_validation.py.
