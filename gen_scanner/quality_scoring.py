"""Quality Scoring — unified scanner v1.0.

15-component quality score (max raw ~130, clamped to 100) for both
LONG (ORH continuation) and SHORT (ORL continuation) setups.

Components mirror each other exactly across directions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time

import pandas as pd

from .config import (
    GRADE_A_PLUS_MIN, GRADE_A_MIN, GRADE_B_MIN,
    SESSION_WINDOW_BONUS, TIMEZONE,
    BEAR_FLAG_MIN_CANDLES, BEAR_FLAG_MAX_CANDLES, BEAR_FLAG_MAX_RANGE_PCT,
    BULL_FLAG_MIN_CANDLES, BULL_FLAG_MAX_CANDLES, BULL_FLAG_MAX_RANGE_PCT,
)
from .utils import safe_float as sf


@dataclass
class QualityResult:
    raw_score: int
    score: int                              # clamped 0-100
    grade: str                              # A+, A, B, Skip
    entry_type: str = ""
    components: dict[str, int] = field(default_factory=dict)
    notes: list[str]           = field(default_factory=list)


def _clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, v))


def _last_bar_time(df5: pd.DataFrame) -> dt_time | None:
    if df5.empty:
        return None
    idx = df5.index[-1]
    if isinstance(idx, pd.Timestamp):
        try:
            if idx.tzinfo is not None:
                idx = idx.tz_convert(TIMEZONE)
            return idx.time()
        except Exception:
            return idx.time()
    return None


def _in_primary_or_best(df5: pd.DataFrame) -> bool:
    t = _last_bar_time(df5)
    return t is not None and dt_time(10, 0) <= t <= dt_time(11, 15)


def score_quality(
    df5: pd.DataFrame,
    df15: pd.DataFrame,
    direction: str,     # "long" or "short"
    or_level: float,    # ORL for short, ORH for long
    nearest_wall: float | None,
    entry_type: str = "",
) -> QualityResult:
    """Compute 15-component quality score for the given direction."""
    if df5.empty:
        return QualityResult(0, 0, "Skip", notes=["no bar data"])

    last  = df5.iloc[-1]
    close = sf(last.get("close"))
    vwap  = sf(last.get("vwap"))
    ema8  = sf(last.get("ema8"))
    ema20 = sf(last.get("ema20"))
    atr14 = sf(last.get("atr14"))

    comp: dict[str, int] = {}
    notes: list[str]     = []

    # ── Mirrored helpers ────────────────────────────────────────────────────
    def is_short() -> bool:
        return direction == "short"

    def price_beyond_or() -> bool:
        return (close < or_level) if is_short() else (close > or_level)

    # ── 1. OR-level breakdown/breakout quality (max 15) ─────────────────────
    below_or = int((df5.tail(5)["close"] < or_level).sum()) if is_short() else \
               int((df5.tail(5)["close"] > or_level).sum())
    comp["or_break_quality"] = 15 if below_or >= 2 else 8 if price_beyond_or() else 2

    # ── 2. Retest / rejection quality (max 15) ───────────────────────────────
    if entry_type == "TYPE_A":
        comp["retest_quality"] = 15
        notes.append("Type A: OR level retest/rejection confirmed")
    elif entry_type == "TYPE_B":
        comp["retest_quality"] = 10
        notes.append("Type B: tight flag base below/above OR level")
    else:
        comp["retest_quality"] = 7
        notes.append("No entry type confirmed — waiting for retest or flag")

    # ── 3. VWAP rejection/support strength (max 12) ──────────────────────────
    if close > 0 and vwap > 0:
        if is_short():
            vwap_gap = (vwap - close) / close   # positive = below VWAP (good for short)
        else:
            vwap_gap = (close - vwap) / close   # positive = above VWAP (good for long)
        if 0 < vwap_gap <= 0.035:
            comp["vwap_strength"] = 12
        elif vwap_gap > 0.035:
            comp["vwap_strength"] = 6
            notes.append("Extended from VWAP — reduced quality")
        else:
            comp["vwap_strength"] = 0
    else:
        comp["vwap_strength"] = 0

    # ── 4. EMA bearish/bullish separation (max 12) ──────────────────────────
    if close > 0:
        ema_gap = abs(ema20 - ema8) / close
        ema_aligned = (ema8 <= ema20) if is_short() else (ema8 >= ema20)
        comp["ema_separation"] = 12 if (ema_gap > 0.002 and ema_aligned) else \
                                   7  if ema_aligned else 0
    else:
        comp["ema_separation"] = 0

    # ── 5. Lower-high / higher-low structure (max 10) ────────────────────────
    recent_pivots = df5.tail(5)["high" if is_short() else "low"].tolist()
    if is_short():
        struct_ok = len(recent_pivots) >= 3 and recent_pivots[-1] <= max(recent_pivots[:-1])
    else:
        struct_ok = len(recent_pivots) >= 3 and recent_pivots[-1] >= min(recent_pivots[:-1])
    comp["or_structure"] = 10 if struct_ok else 4

    # ── 6. Donchian expansion (max 10) ──────────────────────────────────────
    dc_col = "donchian_lower" if is_short() else "donchian_upper"
    if dc_col in df5.columns and len(df5) >= 6:
        dc = df5[dc_col].tail(6).astype(float).tolist()
        dc_slope = (dc[-1] - dc[-4]) if len(dc) >= 4 else 0.0
        earlier  = abs(dc[-3] - dc[-5]) if len(dc) >= 5 else 1.0
        recent_  = abs(dc[-1] - dc[-3])
        stalling = earlier > 0 and recent_ < earlier * 0.25
        if stalling:
            comp["donchian"] = 2
            notes.append("Donchian stalling — trap risk")
        elif (dc_slope < 0 if is_short() else dc_slope > 0):
            comp["donchian"] = 10
        else:
            comp["donchian"] = 4
    else:
        comp["donchian"] = 4

    # ── 7. Volume behavior (max 8) ───────────────────────────────────────────
    tail5    = df5.tail(5)
    bear_vol = tail5.loc[tail5["close"] < tail5["open"], "volume"]
    bull_vol = tail5.loc[tail5["close"] >= tail5["open"], "volume"]
    bear_avg = float(bear_vol.mean()) if not bear_vol.empty else 0.0
    bull_avg = float(bull_vol.mean()) if not bull_vol.empty else 0.0
    if is_short():
        vol_ok = bear_avg > bull_avg * 1.25
    else:
        vol_ok = bull_avg > bear_avg * 1.25
    comp["volume_behavior"] = 8 if vol_ok else 3

    # ── 8. Flag base quality (max 8) ────────────────────────────────────────
    if entry_type == "TYPE_B":
        comp["flag_quality"] = 8
    elif entry_type == "TYPE_A":
        comp["flag_quality"] = 5
    else:
        comp["flag_quality"] = 2

    # ── 9. Room to T1 (max 10) ──────────────────────────────────────────────
    if nearest_wall is not None and close > 0 and nearest_wall != close:
        if is_short():
            room_pct = (close - nearest_wall) / close
        else:
            room_pct = (nearest_wall - close) / close
        if room_pct >= 0.025:
            comp["room_to_t1"] = 10
        elif room_pct >= 0.015:
            comp["room_to_t1"] = 7
        elif room_pct >= 0.008:
            comp["room_to_t1"] = 4
        else:
            comp["room_to_t1"] = 1
            notes.append("T1 very close — limited room")
    else:
        comp["room_to_t1"] = 0
        notes.append("No wall identified — T1 unavailable")

    # ── 10. Fresh continuation (max 8) ──────────────────────────────────────
    recent_lows  = df5.tail(5)["low"].astype(float).tolist()
    recent_highs = df5.tail(5)["high"].astype(float).tolist()
    if is_short():
        fresh = len(recent_lows) >= 2 and recent_lows[-1] < min(recent_lows[:-1])
    else:
        fresh = len(recent_highs) >= 2 and recent_highs[-1] > max(recent_highs[:-1])
    comp["fresh_continuation"] = 8 if fresh else 3

    # ── 11. Directional momentum (max 7) ────────────────────────────────────
    tail5_c = df5.tail(5)
    if is_short():
        dir_count = int((tail5_c["close"] < tail5_c["open"]).sum())
    else:
        dir_count = int((tail5_c["close"] > tail5_c["open"]).sum())
    comp["directional_momentum"] = 7 if dir_count >= 4 else 4 if dir_count >= 3 else 1

    # ── 12. Breakdown/breakout volume (max 8) ────────────────────────────────
    last_vol   = sf(last.get("volume"))
    vol_avg    = sf(last.get("vol_avg"))
    rvol_ratio = (last_vol / vol_avg) if vol_avg > 0 else 0.0
    comp["breakout_volume"] = 8 if rvol_ratio >= 1.5 else 5 if rvol_ratio >= 1.2 else 2

    # ── 13. Gap quality (max 8) ──────────────────────────────────────────────
    try:
        today_open = sf(df5.iloc[0].get("open"))
        prev_close = sf(df5.iloc[0].get("prev_close", 0))
        if prev_close > 0 and today_open > 0:
            gap_pct = (prev_close - today_open) / prev_close  # positive = gap down
            if not is_short():
                gap_pct = -gap_pct                            # positive = gap up for longs
            if gap_pct >= 0.02:
                comp["gap_quality"] = 8
            elif gap_pct >= 0.005:
                comp["gap_quality"] = 5
            elif gap_pct > 0:
                comp["gap_quality"] = 3
            else:
                comp["gap_quality"] = 1
                notes.append("No gap or gap wrong direction")
        else:
            comp["gap_quality"] = 3
    except Exception:
        comp["gap_quality"] = 3

    # ── 14. 15m candle quality (max 8) ───────────────────────────────────────
    if not df15.empty:
        last15 = df15.iloc[-1]
        h15    = sf(last15.get("high"))
        l15    = sf(last15.get("low"))
        o15    = sf(last15.get("open"))
        c15    = sf(last15.get("close"))
        r15    = h15 - l15
        if r15 > 0:
            body = abs(c15 - o15)
            body_pct = body / r15
            if is_short():
                bearish_body = c15 < o15
                comp["candle_15m"] = 8 if (body_pct >= 0.6 and bearish_body) else \
                                      5 if (body_pct >= 0.4 and bearish_body) else 2
            else:
                bullish_body = c15 > o15
                comp["candle_15m"] = 8 if (body_pct >= 0.6 and bullish_body) else \
                                      5 if (body_pct >= 0.4 and bullish_body) else 2
        else:
            comp["candle_15m"] = 3
    else:
        comp["candle_15m"] = 3

    # ── 15. 15m lower-low / higher-high structure (max 6) ────────────────────
    if len(df15) >= 3:
        lows15  = df15.tail(4)["low"].astype(float).tolist()
        highs15 = df15.tail(4)["high"].astype(float).tolist()
        if is_short():
            struct15 = all(lows15[i] <= lows15[i-1] for i in range(1, len(lows15)))
        else:
            struct15 = all(highs15[i] >= highs15[i-1] for i in range(1, len(highs15)))
        comp["structure_15m"] = 6 if struct15 else 2
    else:
        comp["structure_15m"] = 2

    # ── Session window bonus ────────────────────────────────────────────────
    if _in_primary_or_best(df5):
        comp["session_bonus"] = SESSION_WINDOW_BONUS

    # ── Final score + grade ─────────────────────────────────────────────────
    raw_score = sum(comp.values())
    score     = _clamp(raw_score)

    if score >= GRADE_A_PLUS_MIN:
        grade = "A+"
    elif score >= GRADE_A_MIN:
        grade = "A"
    elif score >= GRADE_B_MIN:
        grade = "B"
    else:
        grade = "Skip"

    return QualityResult(
        raw_score=raw_score,
        score=score,
        grade=grade,
        entry_type=entry_type,
        components=comp,
        notes=notes,
    )
