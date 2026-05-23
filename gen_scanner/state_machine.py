"""State Machine + Final Execution Engine — unified scanner v1.0.

State labels (same for both directions):
  TRIGGERED         — entry condition fired, all gates clear, execute
  READY_CONTINUATION — clean setup near entry, waiting for trigger
  WATCHLIST         — structurally valid, not ready yet
  AVOID             — failed a hard gate, skip

Final engine checks (direction-mirrored):
  - Time-window enforcement (spec table)
  - Fresh impulse confirmation
  - Compression / mature drift detection
  - Late-session exhaustion
  - Volume persistence (late session only)
  - T1 realistic check
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dt_time

import pandas as pd

from .config import (
    FINAL_ENGINE_RECENT_BARS, FINAL_ENGINE_MATURE_BARS,
    FINAL_ENGINE_MAX_SIDEWAYS_BARS, FINAL_ENGINE_BREAKDOWN_LOOKBACK,
    FINAL_ENGINE_COMPRESSION_ATR_MULT, FINAL_ENGINE_COMPRESSION_PCT,
    FINAL_ENGINE_MIN_FRESH_ATR_MULT, FINAL_ENGINE_MIN_FRESH_PCT,
    FINAL_ENGINE_MIN_BODY_ATR_MULT, FINAL_ENGINE_MIN_BODY_PCT,
    FINAL_ENGINE_MAX_OR_TRAVEL_ATR_LATE, FINAL_ENGINE_MAX_VWAP_TRAVEL_ATR_LATE,
    FINAL_ENGINE_MAX_TARGET_ATR_LATE, FINAL_ENGINE_MIN_TARGET_ATR,
    FINAL_ENGINE_MIN_VOL_RATIO_LATE, FINAL_ENGINE_LATE_TIME,
    TIMEZONE, RR_MIN,
)
from .utils import safe_float as sf
from .risk_targets import RiskTargetResult


@dataclass
class StateResult:
    state: str          # TRIGGERED / READY_CONTINUATION / WATCHLIST / AVOID
    reason: str


@dataclass
class FinalResult:
    passed: bool
    final_state: str
    grade_override: str | None        = None
    blockers: list[str]               = field(default_factory=list)
    notes: list[str]                  = field(default_factory=list)


# ─── Time helpers ─────────────────────────────────────────────────────────

def _last_time(df: pd.DataFrame) -> dt_time | None:
    if df.empty:
        return None
    idx = df.index[-1]
    if isinstance(idx, pd.Timestamp):
        try:
            if idx.tzinfo is not None:
                idx = idx.tz_convert(TIMEZONE)
            return idx.time()
        except Exception:
            return idx.time()
    return None


def _parse_hhmm(s: str, fallback: dt_time) -> dt_time:
    try:
        h, m = s.split(":", 1)
        return dt_time(int(h), int(m))
    except Exception:
        return fallback


def current_window(df5: pd.DataFrame) -> str:
    t = _last_time(df5)
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


def _is_late(df5: pd.DataFrame) -> bool:
    t = _last_time(df5)
    if t is None:
        return False
    return t >= _parse_hhmm(FINAL_ENGINE_LATE_TIME, dt_time(11, 30))


# ─── Preliminary state assignment ─────────────────────────────────────────

def assign_state(
    df5: pd.DataFrame,
    direction: str,
    or_level: float,   # ORL for short, ORH for long
    grade: str,
    entry_type: str = "",   # FIX H5: passed from struct.entry_type to enable TYPE_B trigger path
) -> StateResult:
    """Assign preliminary state before the final execution engine runs.

    FIX H5: TYPE_A and TYPE_B setups now have separate trigger conditions.
    TYPE_A: OR level retest → rejection → new low/high + volume.
    TYPE_B: tight flag below/above OR level → flag boundary break + volume.
    Prior code applied TYPE_A logic to all setups, leaving TYPE_B setups
    permanently stuck in READY_CONTINUATION even when their proper trigger fired.
    """
    if grade == "Skip":
        return StateResult("AVOID", "grade Skip")
    if len(df5) < 4:
        return StateResult("WATCHLIST", "insufficient bars")

    last    = df5.iloc[-1]
    close   = sf(last.get("close"))
    vol_ref = sf(last.get("vol_avg"))

    def _vol_ok() -> bool:
        if vol_ref <= 0:
            return True
        return (sf(last.get("volume")) / vol_ref) >= 1.20

    if direction == "short":
        if close >= or_level:
            return StateResult("WATCHLIST", "price not below ORL")

        if entry_type == "TYPE_B":
            # TYPE_B trigger: bear flag low broken with volume
            # Flag bars are closes below ORL; we check the last 6.
            flag_bars = df5[df5["close"].astype(float) < or_level].tail(6)
            if len(flag_bars) >= 3:
                flag_low    = float(flag_bars["low"].min())
                broke_flag  = close <= flag_low * 1.003   # at/below flag low (0.3% tolerance)
                if broke_flag and _vol_ok():
                    return StateResult("TRIGGERED", "TYPE_B: bear flag low broken with volume")
            return StateResult("READY_CONTINUATION", "TYPE_B: base forming, awaiting flag-low break")

        else:
            # TYPE_A trigger: ORL retest → new low + volume
            recent      = df5.tail(6)
            retest_seen = bool((recent["high"].astype(float) >= or_level * 0.997).any())
            broke_low   = close <= float(recent["low"].iloc[:-1].min()) if len(recent) > 2 else False
            if retest_seen and broke_low and _vol_ok():
                return StateResult("TRIGGERED", "ORL retest seen + new low + vol confirmed")
            if retest_seen:
                return StateResult("READY_CONTINUATION", "ORL retest seen, awaiting fresh low")
            return StateResult("WATCHLIST", "no ORL retest yet")

    else:  # LONG
        if close <= or_level:
            return StateResult("WATCHLIST", "price not above ORH")

        if entry_type == "TYPE_B":
            # TYPE_B trigger: bull flag high broken with volume
            flag_bars = df5[df5["close"].astype(float) > or_level].tail(6)
            if len(flag_bars) >= 3:
                flag_high   = float(flag_bars["high"].max())
                broke_flag  = close >= flag_high * 0.997   # at/above flag high (0.3% tolerance)
                if broke_flag and _vol_ok():
                    return StateResult("TRIGGERED", "TYPE_B: bull flag high broken with volume")
            return StateResult("READY_CONTINUATION", "TYPE_B: base forming, awaiting flag-high break")

        else:
            # TYPE_A trigger: ORH retest → new high + volume
            recent      = df5.tail(6)
            retest_seen = bool((recent["low"].astype(float) <= or_level * 1.003).any())
            broke_high  = close >= float(recent["high"].iloc[:-1].max()) if len(recent) > 2 else False
            if retest_seen and broke_high and _vol_ok():
                return StateResult("TRIGGERED", "ORH retest seen + new high + vol confirmed")
            if retest_seen:
                return StateResult("READY_CONTINUATION", "ORH retest seen, awaiting fresh high")
            return StateResult("WATCHLIST", "no ORH retest yet")


# ─── Final execution engine checks ───────────────────────────────────────

def _breakdown_bars(df5: pd.DataFrame, or_level: float, direction: str) -> tuple[int, pd.DataFrame]:
    closes = df5["close"].astype(float).tolist()
    first  = None
    for i, c in enumerate(closes):
        if (c < or_level if direction == "short" else c > or_level):
            first = i
            break
    if first is None:
        return 0, df5.tail(0)
    after = df5.iloc[first:].copy()
    return len(after), after


def _fresh_impulse(df5: pd.DataFrame, or_level: float, direction: str) -> tuple[bool, str | None]:
    if len(df5) < 5:
        return False, "Insufficient bars for fresh impulse check"
    atr   = sf(df5.iloc[-1].get("atr14"))
    close = sf(df5.iloc[-1].get("close"))
    if close <= 0:
        return False, "Invalid close"

    lookback     = min(FINAL_ENGINE_BREAKDOWN_LOOKBACK, len(df5) - 1)
    recent_prior = df5.iloc[-lookback - 1:-1]
    fresh_thresh = max(atr * FINAL_ENGINE_MIN_FRESH_ATR_MULT,
                       close * FINAL_ENGINE_MIN_FRESH_PCT, 0.01)

    if direction == "short":
        prior_low  = sf(recent_prior["low"].min()) if not recent_prior.empty else sf(df5.iloc[-2].get("low"))
        last_low   = sf(df5.iloc[-1].get("low"))
        fresh      = last_low < prior_low - fresh_thresh
        recent     = df5.tail(FINAL_ENGINE_RECENT_BARS)
        bodies     = (recent["open"].astype(float) - recent["close"].astype(float)).clip(lower=0)
        body_floor = max(atr * FINAL_ENGINE_MIN_BODY_ATR_MULT, close * FINAL_ENGINE_MIN_BODY_PCT, 0.025)
        bear_cls   = int((recent["close"].astype(float) < recent["open"].astype(float)).sum())
        impulse    = bear_cls >= 2 and float(bodies.sum()) >= body_floor
    else:  # LONG
        prior_high = sf(recent_prior["high"].max()) if not recent_prior.empty else sf(df5.iloc[-2].get("high"))
        last_high  = sf(df5.iloc[-1].get("high"))
        fresh      = last_high > prior_high + fresh_thresh
        recent     = df5.tail(FINAL_ENGINE_RECENT_BARS)
        bodies     = (recent["close"].astype(float) - recent["open"].astype(float)).clip(lower=0)
        body_floor = max(atr * FINAL_ENGINE_MIN_BODY_ATR_MULT, close * FINAL_ENGINE_MIN_BODY_PCT, 0.025)
        bull_cls   = int((recent["close"].astype(float) > recent["open"].astype(float)).sum())
        impulse    = bull_cls >= 2 and float(bodies.sum()) >= body_floor

    if fresh or impulse:
        return True, None
    return False, "Final gate: mature drift / insufficient expansion fuel"


def _compression(df5: pd.DataFrame, or_level: float, direction: str) -> tuple[bool, str | None]:
    if len(df5) < 8:
        return False, None
    atr   = sf(df5.iloc[-1].get("atr14"))
    close = sf(df5.iloc[-1].get("close"))
    if close <= 0 or atr <= 0:
        return False, None
    bars_since, after = _breakdown_bars(df5, or_level, direction)
    if bars_since < 5 or after.empty:
        return False, None
    recent = after.tail(FINAL_ENGINE_MAX_SIDEWAYS_BARS)
    if len(recent) < 4:
        return False, None
    recent_range  = sf(recent["high"].max()) - sf(recent["low"].min())
    comp_floor    = max(atr * FINAL_ENGINE_COMPRESSION_ATR_MULT, close * FINAL_ENGINE_COMPRESSION_PCT, 0.035)
    fresh_thresh  = max(atr * FINAL_ENGINE_MIN_FRESH_ATR_MULT, close * FINAL_ENGINE_MIN_FRESH_PCT, 0.01)
    if direction == "short":
        curr_low  = sf(recent["low"].iloc[-1])
        prior_low = sf(after.iloc[:-1]["low"].min()) if len(after) > 1 else curr_low
        no_fresh  = curr_low >= prior_low - fresh_thresh
    else:
        curr_high  = sf(recent["high"].iloc[-1])
        prior_high = sf(after.iloc[:-1]["high"].max()) if len(after) > 1 else curr_high
        no_fresh   = curr_high <= prior_high + fresh_thresh
    if recent_range <= comp_floor and no_fresh:
        return True, "Final gate: compression after OR break / no fresh expansion"
    if bars_since >= FINAL_ENGINE_MATURE_BARS and no_fresh:
        return True, "Final gate: mature drift / insufficient expansion fuel"
    return False, None


def _late_exhaustion(df5: pd.DataFrame, or_level: float, direction: str) -> tuple[bool, str | None]:
    if not _is_late(df5):
        return False, None
    atr   = sf(df5.iloc[-1].get("atr14"))
    close = sf(df5.iloc[-1].get("close"))
    vwap  = sf(df5.iloc[-1].get("vwap"))
    if atr <= 0 or close <= 0:
        return False, None
    bars_since, _ = _breakdown_bars(df5, or_level, direction)
    if direction == "short":
        travelled_or   = max(0.0, or_level - close)
        travelled_vwap = max(0.0, vwap - close)
    else:
        travelled_or   = max(0.0, close - or_level)
        travelled_vwap = max(0.0, close - vwap)
    if bars_since >= FINAL_ENGINE_MATURE_BARS and travelled_or > atr * FINAL_ENGINE_MAX_OR_TRAVEL_ATR_LATE:
        return True, "Final gate: late mature continuation after extended OR travel"
    if bars_since >= FINAL_ENGINE_MATURE_BARS and travelled_vwap > atr * FINAL_ENGINE_MAX_VWAP_TRAVEL_ATR_LATE:
        return True, "Final gate: late VWAP extension / poor remaining fuel"
    return False, None


def _vol_persistence(df5: pd.DataFrame) -> tuple[bool, str | None]:
    if not _is_late(df5) or len(df5) < 12:
        return False, None
    recent = sf(df5.tail(3)["volume"].mean())
    prior  = sf(df5.tail(12).head(6)["volume"].mean())
    if prior > 0 and recent < prior * FINAL_ENGINE_MIN_VOL_RATIO_LATE:
        return True, "Final gate: late-session volume decay"
    return False, None


def _t1_realistic(df5: pd.DataFrame, risk: RiskTargetResult) -> tuple[bool, str | None]:
    if risk.entry is None or risk.t1 is None:
        return False, "Final gate: no valid entry or T1"
    atr = sf(df5.iloc[-1].get("atr14"))
    if atr <= 0:
        return True, None
    if risk.direction == "short":
        dist = risk.entry - risk.t1
    else:
        dist = risk.t1 - risk.entry
    if dist < atr * FINAL_ENGINE_MIN_TARGET_ATR:
        return False, "Final gate: T1 too close for meaningful follow-through"
    if _is_late(df5) and dist > atr * FINAL_ENGINE_MAX_TARGET_ATR_LATE:
        return False, "Final gate: T1 unlikely to be reached after mature continuation"
    return True, None


# ─── Time-window enforcement ─────────────────────────────────────────────

def _time_gate(df5: pd.DataFrame, prelim_state: str,
               direction: str = "both") -> tuple[bool, str | None]:
    """QC-8: Added direction parameter so Time Rule 2 checks the correct structure.

    Prior code checked `ll` (lower lows) and `lh` (lower highs) for both directions.
    For LONGs, valid structure is HH/HL (higher highs or higher lows).
    For SHORTs, valid structure is LL/LH (lower lows or lower highs).
    Checking direction-agnostic conditions could block valid setups or pass invalid ones.
    """
    window = current_window(df5)
    if window == "EARLY_OBSERVE":
        return True, "Time gate: before 9:45 ET — OR not yet defined, no trades"
    if window == "TRANSITION_GAP":
        return True, "Time gate: 11:15-11:30 transition gap — no new entries"
    if window == "POST_1300_RESTRICTED":
        if prelim_state == "TRIGGERED":
            return False, None   # Fresh triggered expansion allowed
        return True, "Time gate: after 13:00 ET — READY/WATCHLIST blocked"
    if window == "DECISION_WINDOW" and len(df5) >= 4:
        recent_lows  = df5.tail(4)["low"].astype(float).tolist()
        recent_highs = df5.tail(4)["high"].astype(float).tolist()
        last = df5.iloc[-1]
        ema_sep = abs(sf(last.get("ema20")) - sf(last.get("ema8"))) > sf(last.get("close")) * 0.001

        if direction == "short":
            # SHORT: need LL or LH structure (bearish structure forming)
            ll = any(recent_lows[i]  < recent_lows[i-1]  for i in range(1, len(recent_lows)))
            lh = any(recent_highs[i] < recent_highs[i-1] for i in range(1, len(recent_highs)))
            if not (ll or lh) and not ema_sep:
                return True, "Time Rule 2: no LL/LH + EMA flat by 10:15 — range/trap day (short)"
        elif direction == "long":
            # LONG: need HH or HL structure (bullish structure forming)
            hh = any(recent_highs[i] > recent_highs[i-1] for i in range(1, len(recent_highs)))
            hl = any(recent_lows[i]  > recent_lows[i-1]  for i in range(1, len(recent_lows)))
            if not (hh or hl) and not ema_sep:
                return True, "Time Rule 2: no HH/HL + EMA flat by 10:15 — range/trap day (long)"
        else:
            # Fallback: check both (conservative — direction unknown)
            ll = any(recent_lows[i]  < recent_lows[i-1]  for i in range(1, len(recent_lows)))
            lh = any(recent_highs[i] < recent_highs[i-1] for i in range(1, len(recent_highs)))
            if not (ll or lh) and not ema_sep:
                return True, "Time Rule 2: no LL/LH + EMA flat by 10:15 — range/trap day"
    return False, None


# ─── Master final execution eligibility ──────────────────────────────────

def evaluate_final_eligibility(
    df5: pd.DataFrame,
    df15: pd.DataFrame,
    direction: str,
    or_level: float,
    prelim_state: str,
    grade: str,
    risk: RiskTargetResult,
) -> FinalResult:
    if grade == "Skip" or prelim_state == "AVOID":
        return FinalResult(True, prelim_state, None, [],
                           ["final engine skipped: avoid/skip grade"])

    blockers: list[str] = []
    notes: list[str]    = []

    time_blocked, time_msg = _time_gate(df5, prelim_state, direction)   # QC-9: direction passed
    if time_blocked and time_msg:
        blockers.append(time_msg)

    if risk.execution_blockers:
        blockers.extend(risk.execution_blockers)

    t1_ok, t1_msg = _t1_realistic(df5, risk)
    if not t1_ok and t1_msg:
        blockers.append(t1_msg)

    imp_ok, imp_msg = _fresh_impulse(df5, or_level, direction)
    if not imp_ok and imp_msg:
        blockers.append(imp_msg)

    for failed, msg in (
        _compression(df5, or_level, direction),
        _late_exhaustion(df5, or_level, direction),
        _vol_persistence(df5),
    ):
        if failed and msg:
            blockers.append(msg)

    # 15m late-session check
    if _is_late(df5) and len(df15) >= 3:
        recent15 = df15.tail(3)
        if direction == "short":
            ok15 = (sf(recent15["low"].iloc[-1]) < sf(recent15["low"].iloc[:-1].min()) or
                    sf(recent15["close"].iloc[-1]) < sf(recent15["open"].iloc[-1]))
        else:
            ok15 = (sf(recent15["high"].iloc[-1]) > sf(recent15["high"].iloc[:-1].max()) or
                    sf(recent15["close"].iloc[-1]) > sf(recent15["open"].iloc[-1]))
        if not ok15:
            blockers.append("Final gate: no 15m continuation expansion late session")

    blockers = list(dict.fromkeys([b for b in blockers if b]))

    if blockers:
        grade_override = "B" if grade in {"A+", "A"} else None
        return FinalResult(
            passed=False,
            final_state="WATCHLIST",
            grade_override=grade_override,
            blockers=blockers,
            notes=["FINAL_EXECUTION_ENGINE blocked — downgrade locked"],
        )

    return FinalResult(
        passed=True,
        final_state=prelim_state,
        grade_override=None,
        blockers=[],
        notes=["FINAL_EXECUTION_ENGINE clear — eligible for execution"],
    )
