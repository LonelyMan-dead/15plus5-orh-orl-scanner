"""Category A: Structural Validation — unified scanner v1.0.

A single module handles BOTH long (ORH) and short (ORL) structure validation.
Direction is passed as a parameter; the logic is the precise mirror image.

SHORT gates check: below VWAP, EMA bearish, ORL break, red Supertrend, traps
LONG  gates check: above VWAP, EMA bullish, ORH break, green Supertrend, traps
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time

import pandas as pd

from .config import (
    PRICE_MIN, PRICE_MAX, AVG_VOLUME_MIN, SPREAD_MAX_PCT,
    VWAP_FLAT_THRESHOLD_PCT, EMA_SEP_LOOKBACK,
    NEAR_LOW_MAX_PCT, NEAR_HIGH_MAX_PCT,
    LOWER_WICK_MAX_PCT, UPPER_WICK_MAX_PCT,
    TWO_TAP_TOLERANCE_PCT, CONFIRMER_REQUIRED_COUNT, TRAP_BLOCK_COUNT,
    NEAR_SUPPORT_PCT, NEAR_RESISTANCE_PCT, TIMEZONE,
)
from .utils import safe_float as sf, slope as _slope, ema_actively_separating


# ─── Today-session isolation helper ──────────────────────────────────────────
# FIX H2/H3: B10/L10 bar counts and dead-stock volume reference must use
# today's bars only.  yfinance returns up to 5 days of 5m data; prior-day
# bars below/above today's OR level would otherwise contaminate trap checks.

def _today_bars(df5: pd.DataFrame) -> pd.DataFrame:
    """Return only today's RTH bars from a multi-day 5m DataFrame.

    Falls back to last 78 bars (~1 full RTH day) when date isolation fails.
    """
    try:
        if isinstance(df5.index, pd.DatetimeIndex):
            idx = df5.index
            if idx.tzinfo is None:
                idx = idx.tz_localize("UTC").tz_convert(TIMEZONE)
            else:
                idx = idx.tz_convert(TIMEZONE)
            today  = idx.date.max()
            result = df5.loc[idx.date == today]
            if not result.empty:
                return result
    except Exception:
        pass
    return df5.tail(78)  # fallback: ≈1 full RTH day (78 × 5m bars)


@dataclass
class StructuralResult:
    passed: bool
    direction: str                   # "long" or "short"
    reasons: list[str]               = field(default_factory=list)
    hard_rejects: list[str]          = field(default_factory=list)
    orh: float | None                = None
    orl: float | None                = None
    entry_type: str                  = ""   # TYPE_A or TYPE_B
    confirmers_count: int            = 0
    confirmers_list: list[str]       = field(default_factory=list)
    trap_count: int                  = 0
    trap_list: list[str]             = field(default_factory=list)


# ─── Time helpers ─────────────────────────────────────────────────────────

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


def _current_window(df5: pd.DataFrame) -> str:
    t = _last_bar_time(df5)
    if t is None:
        return "UNKNOWN"
    if t < dt_time(9, 45):   return "EARLY_OBSERVE"
    if t < dt_time(10, 0):   return "GATE_WINDOW"
    if t < dt_time(10, 15):  return "DECISION_WINDOW"
    if t < dt_time(10, 30):  return "PRIMARY"
    if t < dt_time(11, 15):  return "BEST"
    if t < dt_time(11, 30):  return "TRANSITION_GAP"
    if t < dt_time(12, 30):  return "SECONDARY"
    if t < dt_time(13, 0):   return "MIDDAY_DEGRADE"
    return "POST_1300_RESTRICTED"


# ─── Shared chop / guard helpers ──────────────────────────────────────────

def _alternating_vwap_chop(df5: pd.DataFrame, lookback: int = 6) -> bool:
    recent = df5.tail(lookback)
    if len(recent) < 4:
        return False
    sides = [1 if c > v else -1
             for c, v in zip(recent["close"].astype(float), recent["vwap"].astype(float))]
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b) >= 3


def _ema_chop(df5: pd.DataFrame, lookback: int = 8) -> bool:
    recent = df5.tail(lookback)
    if len(recent) < 5:
        return False
    above = recent["close"] > recent[["ema8", "ema20"]].max(axis=1)
    below = recent["close"] < recent[["ema8", "ema20"]].min(axis=1)
    neutral = ~(above | below)
    return int(neutral.sum()) >= 3 or (int(above.sum()) >= 2 and int(below.sum()) >= 2)


def _thin_jumpy(df5: pd.DataFrame) -> bool:
    if len(df5) < 5:
        return False
    close = sf(df5.iloc[-1].get("close"))
    if close <= 0:
        return False
    return float(((df5.tail(10)["high"] - df5.tail(10)["low"]) / close).mean()) > 0.08


def _dead_stock(df5: pd.DataFrame) -> bool:
    # FIX H3: use today's bars only for opening-vol reference.
    # Prior: df5.head(6) could reference a prior-day open from a different
    # volatility regime, producing false dead-stock classifications on gap days.
    if len(df5) < 10:
        return False
    tb = _today_bars(df5)
    if len(tb) < 2:
        return False  # insufficient today data — cannot classify, assume live
    # Use first 6 bars of today's session (9:30–10:00 ET opening volume)
    opening_vol = float(tb.iloc[:6]["volume"].mean()) if len(tb) >= 6 else float(tb["volume"].mean())
    recent_vol  = float(df5.tail(10)["volume"].mean())
    if opening_vol <= 0:
        return False
    return recent_vol < opening_vol * 0.40


# ─── SHORT-specific helpers ───────────────────────────────────────────────

def _two_tap_bottom(df5: pd.DataFrame, orl: float) -> bool:
    """Two-tap bottom check with unlock: block returns False once 2 closes below zone."""
    if len(df5) < 6:
        return False
    tol    = max(orl * TWO_TAP_TOLERANCE_PCT, 0.08)
    last10 = df5.tail(10)
    low_zone = float(last10["low"].min())
    taps = last10[last10["low"] <= low_zone + tol]
    if len(taps) < 2:
        return False
    closes_above = int((taps["close"] > low_zone + tol * 0.35).sum())
    last_close   = float(df5.iloc[-1]["close"])
    if closes_above >= 1 and last_close > low_zone + tol * 0.25:
        try:
            tap_idx = last10[last10["low"] <= low_zone + tol].index[-1]
            pos     = df5.index.get_loc(tap_idx)
            after   = df5.iloc[pos + 1:]
        except Exception:
            after = df5.tail(3)
        closes_below = int((after["close"].astype(float) < low_zone - tol * 0.15).sum())
        return closes_below < 2
    return False


def _two_tap_top(df5: pd.DataFrame, orh: float) -> bool:
    """Two-tap top check with unlock (mirror of two_tap_bottom for longs)."""
    if len(df5) < 6:
        return False
    tol    = max(orh * TWO_TAP_TOLERANCE_PCT, 0.08)
    last10 = df5.tail(10)
    high_zone = float(last10["high"].max())
    taps = last10[last10["high"] >= high_zone - tol]
    if len(taps) < 2:
        return False
    closes_below = int((taps["close"] < high_zone - tol * 0.35).sum())
    last_close   = float(df5.iloc[-1]["close"])
    if closes_below >= 1 and last_close < high_zone - tol * 0.25:
        try:
            tap_idx = last10[last10["high"] >= high_zone - tol].index[-1]
            pos     = df5.index.get_loc(tap_idx)
            after   = df5.iloc[pos + 1:]
        except Exception:
            after = df5.tail(3)
        closes_above = int((after["close"].astype(float) > high_zone + tol * 0.15).sum())
        return closes_above < 2
    return False


def _big_dump_no_base(df5: pd.DataFrame, orl: float) -> bool:
    if len(df5) < 6 or "atr14" not in df5.columns:
        return False
    atr_v = sf(df5.iloc[-1].get("atr14"))
    if atr_v <= 0:
        return False
    today_bars = df5.tail(min(len(df5), 42))
    if len(today_bars) < 4:
        return False
    open_price = sf(today_bars.iloc[0].get("open"))
    low_3bar   = float(today_bars.iloc[:3]["low"].min())
    if open_price - low_3bar < atr_v * 1.8:
        return False
    post_dump    = today_bars.iloc[3:]
    if post_dump.empty:
        return True
    recent_range = float(post_dump.tail(4)["high"].max()) - float(post_dump.tail(4)["low"].min())
    return recent_range >= atr_v * 0.8


def _big_spike_no_base(df5: pd.DataFrame, orh: float) -> bool:
    """Mirror of big_dump_no_base for longs."""
    if len(df5) < 6 or "atr14" not in df5.columns:
        return False
    atr_v = sf(df5.iloc[-1].get("atr14"))
    if atr_v <= 0:
        return False
    today_bars = df5.tail(min(len(df5), 42))
    if len(today_bars) < 4:
        return False
    open_price  = sf(today_bars.iloc[0].get("open"))
    high_3bar   = float(today_bars.iloc[:3]["high"].max())
    if high_3bar - open_price < atr_v * 1.8:
        return False
    post_spike   = today_bars.iloc[3:]
    if post_spike.empty:
        return True
    recent_range = float(post_spike.tail(4)["high"].max()) - float(post_spike.tail(4)["low"].min())
    return recent_range >= atr_v * 0.8


def _violent_reclaim(df5: pd.DataFrame, orl: float) -> bool:
    """Large bullish candle reclaiming ORL or VWAP — kills short thesis."""
    if len(df5) < 3:
        return False
    for _, bar in df5.tail(4).iterrows():
        o, c = sf(bar.get("open")), sf(bar.get("close"))
        h, l, vwap = sf(bar.get("high")), sf(bar.get("low")), sf(bar.get("vwap"))
        if c <= o:
            continue
        bar_range = h - l
        if bar_range <= 0:
            continue
        if (c - o) / bar_range >= 0.55 and (c > orl or c > vwap):
            return True
    return False


def _violent_selloff(df5: pd.DataFrame, orh: float) -> bool:
    """Large bearish candle selling off through ORH or VWAP — kills long thesis."""
    if len(df5) < 3:
        return False
    for _, bar in df5.tail(4).iterrows():
        o, c = sf(bar.get("open")), sf(bar.get("close"))
        h, l, vwap = sf(bar.get("high")), sf(bar.get("low")), sf(bar.get("vwap"))
        if c >= o:
            continue
        bar_range = h - l
        if bar_range <= 0:
            continue
        if (o - c) / bar_range >= 0.55 and (c < orh or c < vwap):
            return True
    return False


# ─── SHORT confirmers & traps ──────────────────────────────────────────────

def _short_confirmers(df5: pd.DataFrame, orl: float) -> tuple[int, list[str]]:
    present: list[str] = []
    # C1: Donchian lower band expanding
    if "donchian_lower" in df5.columns and len(df5) >= 4:
        dc = df5["donchian_lower"].tail(4).astype(float).tolist()
        if dc[-1] < dc[0]:
            present.append("donchian_expanding_down")
    # C2: Clean EMA pullback with rejection
    for _, row in df5.tail(5).iterrows():
        ema_lo = min(sf(row.get("ema8")), sf(row.get("ema20")))
        if float(row.get("high", 0)) >= ema_lo * 0.998 and float(row.get("close", 0)) < ema_lo:
            present.append("ema_pullback_rejection")
            break
    # C3: Red vol > green vol (spec target: 1.25× — aligned with quality_scoring component 7)
    tail5    = df5.tail(5)
    red_vols = tail5.loc[tail5["close"] < tail5["open"], "volume"]
    grn_vols = tail5.loc[tail5["close"] >= tail5["open"], "volume"]
    red_avg  = float(red_vols.mean()) if not red_vols.empty else 0.0
    grn_avg  = float(grn_vols.mean()) if not grn_vols.empty else float("inf")
    if red_avg > grn_avg * 1.25:   # FIX 3.4: 1.25× threshold matches quality_scoring
        present.append("bearish_volume_pattern")
    # C4: No double-bottom
    if not _two_tap_bottom(df5, orl):
        present.append("no_double_bottom")
    return len(present), present


def _short_traps(df5: pd.DataFrame, orl: float,
                 pivots: dict[str, float] | None = None) -> tuple[int, list[str]]:
    traps: list[str] = []
    last    = df5.iloc[-1]
    close   = sf(last.get("close"))
    atr_val = sf(last.get("atr14"))

    # B1: Donchian stall after expansion
    if "donchian_lower" in df5.columns and len(df5) >= 5:
        dc = df5["donchian_lower"].tail(5).astype(float).tolist()
        earlier = abs(dc[-3] - dc[-5]) if len(dc) >= 5 else 1
        recent_ = abs(dc[-1] - dc[-3])
        if earlier > 0 and recent_ < earlier * 0.20:
            traps.append("B1_donchian_stall")
    # B2: Two-tap bottom
    if _two_tap_bottom(df5, orl):
        traps.append("B2_two_tap_bottom")
    # B3: VWAP flat/rising
    vwap_slope = _slope(df5["vwap"], 3)
    flat_floor = close * VWAP_FLAT_THRESHOLD_PCT if close else 0.001
    if vwap_slope >= -flat_floor:
        traps.append("B3_vwap_flat_or_rising")
    # B4: Green vol > red vol
    tail5    = df5.tail(5)
    rv       = float(tail5.loc[tail5["close"] < tail5["open"], "volume"].mean()) if not tail5.loc[tail5["close"] < tail5["open"]].empty else 0.0
    gv       = float(tail5.loc[tail5["close"] >= tail5["open"], "volume"].mean()) if not tail5.loc[tail5["close"] >= tail5["open"]].empty else 0.0
    if gv > rv * 1.1:
        traps.append("B4_green_vol_dominates")
    # B5: VWAP reclaim persistent
    if len(df5) >= 6:
        reclaims = int((df5.tail(6)["close"].astype(float) > df5.tail(6)["vwap"].astype(float)).sum())
        if reclaims >= 3:
            traps.append("B5_vwap_reclaim_persistent")
    # B6: EMA flattening
    if len(df5) >= 5:
        gaps = (df5["ema20"].astype(float) - df5["ema8"].astype(float)).tail(4).tolist()
        if len(gaps) >= 3 and max(gaps) > 0 and gaps[-1] < max(gaps) * 0.25:
            traps.append("B6_ema_flattening")
    # B7: Big dump no base
    if _big_dump_no_base(df5, orl):
        traps.append("B7_big_dump_no_base")
    # B8: Directly above real pivot support (0.4% proximity) — includes round numbers per spec
    # FIX H4: round numbers (ROUND_DN1/DN2) were previously excluded, silently allowing
    # shorts too close to $X.00 levels.  Spec treats round numbers as first-class key levels.
    if pivots and close > 0:
        real_walls = {k: v for k, v in pivots.items() if v < close}
        if real_walls:
            nearest = max(real_walls.values())
            if 0 < (close - nearest) / close < 0.004:
                traps.append("B8_directly_above_support")
    # B9: Sideways compression
    if atr_val > 0 and len(df5) >= 6:
        recent_range = float(df5.tail(6)["high"].max()) - float(df5.tail(6)["low"].min())
        if recent_range < atr_val * 0.55:
            traps.append("B9_sideways_compression")
    # B10: Mature breakdown (12+ bars below ORL — today's session only)
    # FIX H2: prior code counted ALL bars in df5 including prior-day bars,
    # causing false B10 activation when prior-day price was below today's ORL.
    _today_df_short = _today_bars(df5)
    if int((_today_df_short["close"].astype(float) < orl).sum()) >= 12:
        traps.append("B10_mature_breakdown")
    return len(traps), traps


# ─── LONG confirmers & traps ─────────────────────────────────────────────

def _long_confirmers(df5: pd.DataFrame, orh: float) -> tuple[int, list[str]]:
    present: list[str] = []
    # C1: Donchian upper band expanding
    if "donchian_upper" in df5.columns and len(df5) >= 4:
        dc = df5["donchian_upper"].tail(4).astype(float).tolist()
        if dc[-1] > dc[0]:
            present.append("donchian_expanding_up")
    # C2: Clean EMA pullback holding at EMAs
    for _, row in df5.tail(5).iterrows():
        ema_lo = min(sf(row.get("ema8")), sf(row.get("ema20")))
        lo     = sf(row.get("low"))
        cl     = sf(row.get("close"))
        if lo <= max(sf(row.get("ema8")), sf(row.get("ema20"))) * 1.002 and cl > ema_lo:
            present.append("pullback_held_ema")
            break
    # C3: Green vol > red vol (spec symmetry: 1.25× threshold matches quality_scoring component 7)
    tail5    = df5.tail(5)
    grn_vols = tail5.loc[tail5["close"] > tail5["open"], "volume"]
    red_vols = tail5.loc[tail5["close"] <= tail5["open"], "volume"]
    grn_avg  = float(grn_vols.mean()) if not grn_vols.empty else 0.0
    red_avg  = float(red_vols.mean()) if not red_vols.empty else float("inf")
    if grn_avg > red_avg * 1.25:   # FIX 3.4: 1.25× threshold aligned with quality_scoring
        present.append("bullish_volume_pattern")
    # C4: No double-top
    if not _two_tap_top(df5, orh):
        present.append("no_double_top")
    return len(present), present


def _long_traps(df5: pd.DataFrame, orh: float,
                pivots: dict[str, float] | None = None) -> tuple[int, list[str]]:
    traps: list[str] = []
    last    = df5.iloc[-1]
    close   = sf(last.get("close"))
    atr_val = sf(last.get("atr14"))

    # L1: Donchian stall after expansion
    if "donchian_upper" in df5.columns and len(df5) >= 5:
        dc = df5["donchian_upper"].tail(5).astype(float).tolist()
        earlier = abs(dc[-3] - dc[-5]) if len(dc) >= 5 else 1
        recent_ = abs(dc[-1] - dc[-3])
        if earlier > 0 and recent_ < earlier * 0.20:
            traps.append("L1_donchian_stall")
    # L2: Two-tap top
    if _two_tap_top(df5, orh):
        traps.append("L2_two_tap_top")
    # L3: VWAP flat/falling
    vwap_slope = _slope(df5["vwap"], 3)
    flat_floor = close * VWAP_FLAT_THRESHOLD_PCT if close else 0.001
    if vwap_slope <= flat_floor:
        traps.append("L3_vwap_flat_or_falling")
    # L4: Red vol > green vol
    tail5    = df5.tail(5)
    rv       = float(tail5.loc[tail5["close"] < tail5["open"], "volume"].mean()) if not tail5.loc[tail5["close"] < tail5["open"]].empty else 0.0
    gv       = float(tail5.loc[tail5["close"] >= tail5["open"], "volume"].mean()) if not tail5.loc[tail5["close"] >= tail5["open"]].empty else 0.0
    if rv > gv * 1.1:
        traps.append("L4_red_vol_dominates")
    # L5: VWAP loss persistent
    if len(df5) >= 6:
        losses = int((df5.tail(6)["close"].astype(float) < df5.tail(6)["vwap"].astype(float)).sum())
        if losses >= 3:
            traps.append("L5_vwap_loss_persistent")
    # L6: EMA flattening
    if len(df5) >= 5:
        gaps = (df5["ema8"].astype(float) - df5["ema20"].astype(float)).tail(4).tolist()
        if len(gaps) >= 3 and max(gaps) > 0 and gaps[-1] < max(gaps) * 0.25:
            traps.append("L6_ema_flattening")
    # L7: Big spike no base
    if _big_spike_no_base(df5, orh):
        traps.append("L7_big_spike_no_base")
    # L8: Directly below real pivot resistance (0.4% proximity) — includes round numbers per spec
    # FIX H4: round numbers (ROUND_UP1/UP2) were previously excluded, silently allowing
    # longs too close to $X.00 resistance.  Spec treats round numbers as first-class key levels.
    if pivots and close > 0:
        real_walls = {k: v for k, v in pivots.items() if v > close}
        if real_walls:
            nearest = min(real_walls.values())
            if 0 < (nearest - close) / close < 0.004:
                traps.append("L8_directly_below_resistance")
    # L9: Sideways compression after breakout
    if atr_val > 0 and len(df5) >= 6:
        recent_range = float(df5.tail(6)["high"].max()) - float(df5.tail(6)["low"].min())
        if recent_range < atr_val * 0.55:
            traps.append("L9_sideways_compression")
    # L10: Mature breakout (12+ bars above ORH — today's session only)
    # FIX H2: prior code counted ALL bars in df5 including prior-day bars,
    # causing false L10 activation when prior-day price was above today's ORH.
    _today_df_long = _today_bars(df5)
    if int((_today_df_long["close"].astype(float) > orh).sum()) >= 12:
        traps.append("L10_mature_breakout")
    return len(traps), traps


# ─── After-10:30 strict mode (6 conditions — both directions) ────────────

def _strict_mode_check(df5: pd.DataFrame, df15: pd.DataFrame,
                        or_level: float, direction: str) -> tuple[bool, str]:
    t = _last_bar_time(df5)
    if t is None or t < dt_time(10, 30):
        return False, ""
    last  = df5.iloc[-1]
    close = sf(last.get("close"))
    vwap  = sf(last.get("vwap"))
    ema20 = sf(last.get("ema20"))
    fails: list[str] = []

    if direction == "short":
        if close >= vwap:   fails.append("price not below VWAP")
        if close >= ema20:  fails.append("price not below EMA20")
        if "supertrend_bullish" in df15.columns and bool(df15.iloc[-1]["supertrend_bullish"]):
            fails.append("15m Supertrend not RED")
        if "donchian_lower" in df5.columns and len(df5) >= 4:
            dc_slope = float(df5["donchian_lower"].iloc[-1] - df5["donchian_lower"].iloc[-4])
            if dc_slope >= 0:
                fails.append("Donchian lower not falling")
        if (df5.tail(3)["close"].astype(float) > df5.tail(3)["vwap"].astype(float)).any():
            fails.append("VWAP reclaim in last 3 bars")
        atr_v = sf(last.get("atr14"))
        recent_lows = df5.tail(5)["low"].astype(float).tolist()
        fresh_low = len(recent_lows) >= 2 and recent_lows[-1] < min(recent_lows[:-1]) - max(atr_v * 0.15, 0.01)
        red_count = int((df5.tail(4)["close"].astype(float) < df5.tail(4)["open"].astype(float)).sum())
        if not fresh_low and red_count < 2:
            fails.append("no fresh downside continuation")
    else:  # long
        if close <= vwap:   fails.append("price not above VWAP")
        if close <= ema20:  fails.append("price not above EMA20")
        if "supertrend_bullish" in df15.columns and not bool(df15.iloc[-1]["supertrend_bullish"]):
            fails.append("15m Supertrend not GREEN")
        if "donchian_upper" in df5.columns and len(df5) >= 4:
            dc_slope = float(df5["donchian_upper"].iloc[-1] - df5["donchian_upper"].iloc[-4])
            if dc_slope <= 0:
                fails.append("Donchian upper not rising")
        if (df5.tail(3)["close"].astype(float) < df5.tail(3)["vwap"].astype(float)).any():
            fails.append("VWAP loss in last 3 bars")
        atr_v = sf(last.get("atr14"))
        recent_highs = df5.tail(5)["high"].astype(float).tolist()
        fresh_high = len(recent_highs) >= 2 and recent_highs[-1] > max(recent_highs[:-1]) + max(atr_v * 0.15, 0.01)
        grn_count = int((df5.tail(4)["close"].astype(float) > df5.tail(4)["open"].astype(float)).sum())
        if not fresh_high and grn_count < 2:
            fails.append("no fresh upside continuation")

    if fails:
        return True, f"10:30+ strict mode ({len(fails)}/6 conditions fail): {'; '.join(fails)}"
    return False, ""


# ─── Master validate_structure ────────────────────────────────────────────

def validate_structure(
    symbol: str,
    df5: pd.DataFrame,
    df15: pd.DataFrame,
    direction: str,         # "long" or "short"
    avg_volume: int,
    avg_spread_pct: float,
    orh: float,
    orl: float,
    nearest_wall: float | None = None,
    pivots: dict[str, float] | None = None,
) -> StructuralResult:
    """Unified structure validation for both long and short setups."""
    result = StructuralResult(passed=True, direction=direction,
                               orh=orh, orl=orl)
    if df5.empty or df15.empty:
        result.passed = False
        result.hard_rejects.append("insufficient bar data")
        return result

    last   = df5.iloc[-1]
    close  = sf(last.get("close"))
    vwap_v = sf(last.get("vwap"))
    ema8   = sf(last.get("ema8"))
    ema20  = sf(last.get("ema20"))

    rejects = result.hard_rejects

    # ── Universe hard gates (shared) ─────────────────────────────────────
    if not (PRICE_MIN <= close <= PRICE_MAX):
        rejects.append("price outside universe")
    if avg_volume < AVG_VOLUME_MIN:
        rejects.append("avg volume below floor")
    if avg_spread_pct > SPREAD_MAX_PCT:
        rejects.append("spread too wide")
    if _thin_jumpy(df5):
        rejects.append("thin/jumpy candles")
    if _dead_stock(df5):
        rejects.append("dead stock: volume decayed >60%")

    # ── Direction-specific core gates ────────────────────────────────────
    vwap_slope = _slope(df5["vwap"], 3)
    flat_floor = close * VWAP_FLAT_THRESHOLD_PCT if close else 0.001

    if direction == "short":
        if close >= vwap_v:
            rejects.append("price not below VWAP")
        if vwap_slope > 0:
            rejects.append("VWAP rising — no short continuation")
        elif abs(vwap_slope) <= flat_floor:
            rejects.append("VWAP flat — no short continuation")
        if close > orl and float(df5.tail(3)["close"].min()) > orl:
            rejects.append("no ORL break — price still above ORL")
        if not (ema8 <= ema20 or (ema20 - ema8) < close * 0.001):
            rejects.append("EMA alignment not bearish (EMA8 > EMA20)")
        elif not ema_actively_separating(df5, EMA_SEP_LOOKBACK, "bearish"):
            rejects.append("EMA not actively widening (bearish)")
        # 15m structure: no higher high after OR
        post_or = df15.iloc[1:] if len(df15) > 1 else df15.iloc[0:0]
        if not post_or.empty and float(post_or["high"].max()) > orh:
            rejects.append("15m higher high after OR (bearish structure broken)")
        if not df15.empty:
            last15 = df15.iloc[-1]
            r15    = sf(last15.get("high")) - sf(last15.get("low"))
            if r15 > 0:
                close_pct = (sf(last15.get("close")) - sf(last15.get("low"))) / r15
                if close_pct > NEAR_LOW_MAX_PCT:
                    rejects.append(f"15m close not near low ({close_pct:.0%} > {NEAR_LOW_MAX_PCT:.0%})")
            lower_wick = sf(last15.get("low")) - min(sf(last15.get("open")), sf(last15.get("close")))
            if r15 > 0 and lower_wick / r15 > LOWER_WICK_MAX_PCT:
                rejects.append("15m large lower wick — buyer pressure")
        if "supertrend_bullish" in df15.columns and bool(df15.iloc[-1]["supertrend_bullish"]):
            rejects.append("15m Supertrend GREEN — Gate 3 fail (no short)")
        if _alternating_vwap_chop(df5):
            rejects.append("VWAP chop (alternating sides)")
        if _ema_chop(df5):
            rejects.append("EMA chop")
        if _violent_reclaim(df5, orl):
            rejects.append("violent reclaim candle — buyers overwhelmed short thesis")
        if _two_tap_bottom(df5, orl):
            rejects.append("two-tap bottom — wait for 2 full closes below zone")
        # Donchian Gate 4 short — reads from 15m DC 20 per spec
        # FIX H1: prior code read from df5 (5m DC 10).  Spec Gate 4 explicitly
        # references the 15m Donchian 20 channel.  Using 5m DC 10 created a
        # faster/noisier channel that diverged from what the trader sees on chart.
        if not df15.empty and "donchian_lower" in df15.columns and "donchian_upper" in df15.columns:
            last15_g4 = df15.iloc[-1]
            dc_lo  = sf(last15_g4.get("donchian_lower"))
            dc_up  = sf(last15_g4.get("donchian_upper"))
            dc_range = dc_up - dc_lo
            if dc_range > 0:
                dc_pos = (close - dc_lo) / dc_range
                if dc_pos >= 0.50:
                    rejects.append(f"Donchian (15m DC20): price in upper half ({dc_pos:.0%}) — weak short")
                elif dc_pos >= 0.30 and abs(vwap_slope) <= flat_floor:
                    rejects.append("Donchian (15m DC20): mid-channel + flat VWAP — range, skip")
        # Gate 5: room to target
        if nearest_wall is not None and orl > nearest_wall:
            room_pct = (orl - nearest_wall) / orl
            if room_pct < NEAR_SUPPORT_PCT:
                rejects.append("Gate 5: ORL too close to nearest support — skip")
        if not rejects:
            # Permission Gate 1: VWAP/ORL hold
            gate1_ok = False
            if int((df5.tail(5)["close"].astype(float) < orl).sum()) >= 2:
                gate1_ok = True
            closes = df5["close"].astype(float).tolist()
            vwaps  = df5["vwap"].astype(float).tolist()
            for i in range(len(df5) - 1):
                if closes[i] < vwaps[i] and closes[i+1] < vwaps[i+1]:
                    gate1_ok = True
                    break
            for _, row in df5.tail(5).iterrows():
                if sf(row.get("high")) > sf(row.get("vwap")) and sf(row.get("close")) < sf(row.get("vwap")):
                    gate1_ok = True
                    break
            if not gate1_ok:
                rejects.append("Permission Gate 1: VWAP/ORL hold condition not met")
        if not rejects:
            conf_count, conf_list = _short_confirmers(df5, orl)
            result.confirmers_count = conf_count
            result.confirmers_list  = conf_list
            if conf_count < CONFIRMER_REQUIRED_COUNT:
                rejects.append(f"Confirmers gate: only {conf_count}/{CONFIRMER_REQUIRED_COUNT} present ({', '.join(conf_list) or 'none'})")
        trap_count, trap_list = _short_traps(df5, orl, pivots)
        result.trap_count = trap_count
        result.trap_list  = trap_list
        if trap_count >= TRAP_BLOCK_COUNT:
            rejects.append(f"Liquidity trap: {trap_count} conditions active ({', '.join(trap_list[:4])})")
        # Entry type classification
        if close < orl:
            if bool((df5.tail(6)["high"].astype(float) >= orl * 0.997).any()):
                result.entry_type = "TYPE_A"
            else:
                below = df5[df5["close"].astype(float) < orl].tail(6)
                if len(below) >= 3:
                    rng = float(below["high"].max()) - float(below["low"].min())
                    ref = sf(below.iloc[-1].get("close"))
                    ema20_v = sf(below.iloc[-1].get("ema20"))
                    base_below = ema20_v <= 0 or float(below["close"].max()) < ema20_v
                    if ref > 0 and (rng / ref) <= 0.018 and base_below:
                        result.entry_type = "TYPE_B"

    else:  # LONG
        if close <= vwap_v:
            rejects.append("price not above VWAP")
        if vwap_slope < 0:
            rejects.append("VWAP falling — no long continuation")
        elif abs(vwap_slope) <= flat_floor:
            rejects.append("VWAP flat — no long continuation")
        if close < orh and float(df5.tail(3)["close"].max()) < orh:
            rejects.append("no ORH break — price still below ORH")
        # ── EMA Qualifier — OR gate per spec "Choose at Least One" ────────
        # FIX H8: prior code required BOTH alignment AND active separation (AND gate).
        # Spec says "Choose at Least One" from three conditions (OR gate).
        # Active separation is measured in quality_scoring component 4 — it is
        # a quality signal, not a hard structural gate.
        ema_cond1 = ema8 >= ema20 or (ema8 - ema20) > -(close * 0.001)  # C1: EMA 8 ≥ EMA 20
        ema20_slope = _slope(df5["ema20"], EMA_SEP_LOOKBACK) if "ema20" in df5.columns else 0.0
        ema_cond2 = ema20_slope >= 0.0                                    # C2: EMA 20 flat-to-rising
        ema_cond3 = not _ema_chop(df5)                                    # C3: price not chopping through EMAs
        if not (ema_cond1 or ema_cond2 or ema_cond3):
            rejects.append("EMA qualifier: EMA8 below EMA20, EMA20 declining, and EMA chop — all 3 fail")
        # 15m structure: no lower low after OR
        post_or = df15.iloc[1:] if len(df15) > 1 else df15.iloc[0:0]
        if not post_or.empty and float(post_or["low"].min()) < orl:
            rejects.append("15m lower low after OR (bullish structure broken)")
        if not df15.empty:
            last15 = df15.iloc[-1]
            r15    = sf(last15.get("high")) - sf(last15.get("low"))
            if r15 > 0:
                close_pct = (sf(last15.get("high")) - sf(last15.get("close"))) / r15
                if close_pct > (1.0 - NEAR_HIGH_MAX_PCT):
                    rejects.append(f"15m close not near high ({(1-close_pct):.0%} from top)")
            upper_wick = sf(last15.get("high")) - max(sf(last15.get("open")), sf(last15.get("close")))
            if r15 > 0 and upper_wick / r15 > UPPER_WICK_MAX_PCT:
                rejects.append("15m large upper wick — seller pressure")
        if "supertrend_bullish" in df15.columns and not bool(df15.iloc[-1]["supertrend_bullish"]):
            rejects.append("15m Supertrend RED — Gate 3 fail (no long)")
        if _alternating_vwap_chop(df5):
            rejects.append("VWAP chop (alternating sides)")
        if _ema_chop(df5):
            rejects.append("EMA chop")
        if _violent_selloff(df5, orh):
            rejects.append("violent sell-off candle — sellers overwhelmed long thesis")
        if _two_tap_top(df5, orh):
            rejects.append("two-tap top — wait for 2 full closes above zone")
        # Donchian Gate 4 long — reads from 15m DC 20 per spec
        # FIX H1: prior code read from df5 (5m DC 10).  Spec Gate 4 explicitly
        # references the 15m Donchian 20 channel.  Using 5m DC 10 created a
        # faster/noisier channel that diverged from what the trader sees on chart.
        if not df15.empty and "donchian_lower" in df15.columns and "donchian_upper" in df15.columns:
            last15_g4 = df15.iloc[-1]
            dc_lo  = sf(last15_g4.get("donchian_lower"))
            dc_up  = sf(last15_g4.get("donchian_upper"))
            dc_range = dc_up - dc_lo
            if dc_range > 0:
                dc_pos = (close - dc_lo) / dc_range
                if dc_pos < 0.50:
                    rejects.append(f"Donchian (15m DC20): price in lower half ({dc_pos:.0%}) — weak long")
                elif dc_pos <= 0.70 and abs(vwap_slope) <= flat_floor:
                    rejects.append("Donchian (15m DC20): mid-channel + flat VWAP — range, skip")
        # Gate 5: room to target above ORH
        if nearest_wall is not None and nearest_wall > orh:
            room_pct = (nearest_wall - orh) / orh if orh else 0
            if room_pct < NEAR_RESISTANCE_PCT:
                rejects.append("Gate 5: ORH too close to nearest resistance — skip")
        if not rejects:
            # Permission Gate 1 long: VWAP/ORH hold
            gate1_ok = False
            if int((df5.tail(5)["close"].astype(float) > orh).sum()) >= 2:
                gate1_ok = True
            closes = df5["close"].astype(float).tolist()
            vwaps  = df5["vwap"].astype(float).tolist()
            for i in range(len(df5) - 1):
                if closes[i] > vwaps[i] and closes[i+1] > vwaps[i+1]:
                    gate1_ok = True
                    break
            if not gate1_ok:
                rejects.append("Permission Gate 1: VWAP/ORH hold condition not met")
        if not rejects:
            conf_count, conf_list = _long_confirmers(df5, orh)
            result.confirmers_count = conf_count
            result.confirmers_list  = conf_list
            if conf_count < CONFIRMER_REQUIRED_COUNT:
                rejects.append(f"Confirmers gate: only {conf_count}/{CONFIRMER_REQUIRED_COUNT} present ({', '.join(conf_list) or 'none'})")
        trap_count, trap_list = _long_traps(df5, orh, pivots)
        result.trap_count = trap_count
        result.trap_list  = trap_list
        if trap_count >= TRAP_BLOCK_COUNT:
            rejects.append(f"Fade risk: {trap_count} conditions active ({', '.join(trap_list[:4])})")
        # Entry type classification
        if close > orh:
            if bool((df5.tail(6)["low"].astype(float) <= orh * 1.003).any()):
                result.entry_type = "TYPE_A"
            else:
                above = df5[df5["close"].astype(float) > orh].tail(6)
                if len(above) >= 3:
                    rng = float(above["high"].max()) - float(above["low"].min())
                    ref = sf(above.iloc[-1].get("close"))
                    ema20_v = sf(above.iloc[-1].get("ema20"))
                    base_above = ema20_v <= 0 or float(above["close"].min()) > ema20_v
                    if ref > 0 and (rng / ref) <= 0.018 and base_above:
                        result.entry_type = "TYPE_B"

    # ── After-10:30 strict mode ──────────────────────────────────────────
    or_level = orl if direction == "short" else orh
    strict_blocked, strict_msg = _strict_mode_check(df5, df15, or_level, direction)
    if strict_blocked:
        rejects.append(strict_msg)

    if rejects:
        result.passed  = False
        result.reasons = rejects.copy()
    else:
        result.reasons = [f"{direction.upper()} structural pass — all gates cleared"]
    return result
