"""Risk Targets — unified scanner v1.0.

Handles survivable stop construction and R:R calculation for both
LONG (stop below retest low) and SHORT (stop above failed reclaim high).

Stop hierarchy:
  SHORT: failed reclaim high → ORL band → VWAP sweep zone → EMA cluster → compression ceiling → wick cluster
  LONG:  failed breakdown low → ORH support zone → VWAP bounce zone → EMA cluster → compression floor → wick cluster
"""
from __future__ import annotations

from dataclasses import dataclass, field
import pandas as pd

from .config import (
    ATR_STOP_BUFFER_MULT, PCT_STOP_BUFFER,
    MIN_STOP_NOISE_PCT, MIN_STOP_ATR_MULT, MIN_STOP_RANGE_MULT,
    RR_MIN,
)
from .utils import safe_float as sf


@dataclass
class RiskTargetResult:
    direction: str
    entry: float | None
    stop: float | None
    t1: float | None
    t2: float | None
    rr: float | None
    t1_label: str | None
    notes: list[str]
    execution_blockers: list[str] = field(default_factory=list)
    suggested_shares: int = 0


# ─── Wall finders ─────────────────────────────────────────────────────────

def nearest_downside_wall(close: float, pivots: dict[str, float]) -> tuple[str | None, float | None]:
    below = [(name, level) for name, level in pivots.items() if level < close]
    if not below:
        return None, None
    return max(below, key=lambda x: x[1])


def nearest_upside_wall(close: float, pivots: dict[str, float]) -> tuple[str | None, float | None]:
    above = [(name, level) for name, level in pivots.items() if level > close]
    if not above:
        return None, None
    return min(above, key=lambda x: x[1])


def second_downside_wall(close: float, pivots: dict, first: float | None) -> tuple[str | None, float | None]:
    below = [(n, v) for n, v in pivots.items() if v < close and (first is None or v < first)]
    if not below:
        return None, None
    return max(below, key=lambda x: x[1])


def second_upside_wall(close: float, pivots: dict, first: float | None) -> tuple[str | None, float | None]:
    above = [(n, v) for n, v in pivots.items() if v > close and (first is None or v > first)]
    if not above:
        return None, None
    return min(above, key=lambda x: x[1])


# ─── Stop construction helpers ────────────────────────────────────────────

def _noise_floor(df5: pd.DataFrame, close: float, atr: float) -> float:
    recent    = df5.tail(8)
    avg_range = sf((recent["high"] - recent["low"]).mean()) if not recent.empty else 0.0
    return max(
        atr * MIN_STOP_ATR_MULT,
        avg_range * MIN_STOP_RANGE_MULT,
        close * MIN_STOP_NOISE_PCT,
        0.03,
    )


def _failed_reclaim_high(df5: pd.DataFrame, orl: float) -> float | None:
    """Highest wick from bars that specifically tested and failed a short resistance."""
    if len(df5) < 3:
        return None
    highs: list[float] = []
    for _, bar in df5.tail(12).iterrows():
        high  = sf(bar.get("high"))
        close = sf(bar.get("close"))
        vwap  = sf(bar.get("vwap"))
        ema8  = sf(bar.get("ema8"))
        ema20 = sf(bar.get("ema20"))
        for lvl in [orl, vwap, ema8, ema20]:
            if lvl <= 0:
                continue
            if (abs(high - lvl) <= lvl * 0.003 or high >= lvl) and close < lvl:
                highs.append(high)
                break
    return max(highs) if highs else None


def _failed_breakdown_low(df5: pd.DataFrame, orh: float) -> float | None:
    """Lowest wick from bars that specifically tested and failed a long support."""
    if len(df5) < 3:
        return None
    lows: list[float] = []
    for _, bar in df5.tail(12).iterrows():
        low   = sf(bar.get("low"))
        close = sf(bar.get("close"))
        vwap  = sf(bar.get("vwap"))
        ema8  = sf(bar.get("ema8"))
        ema20 = sf(bar.get("ema20"))
        for lvl in [orh, vwap, ema8, ema20]:
            if lvl <= 0:
                continue
            if (abs(low - lvl) <= lvl * 0.003 or low <= lvl) and close > lvl:
                lows.append(low)
                break
    return min(lows) if lows else None


def _wick_cluster(df5: pd.DataFrame, side: str = "high") -> float | None:
    """True cluster: highest/lowest wick within a tight band around the median."""
    if len(df5) < 8:
        return None
    vals   = df5.tail(8)[side].astype(float).tolist()
    median = sorted(vals)[len(vals) // 2]
    band   = median * 0.005
    cluster = [v for v in vals if abs(v - median) <= band]
    if not cluster:
        return median
    return max(cluster) if side == "high" else min(cluster)


# ─── Master risk/target calculator ───────────────────────────────────────

def calculate_risk_targets(
    df5: pd.DataFrame,
    direction: str,
    or_level: float,          # ORL for short, ORH for long
    pivots: dict[str, float],
    account_size: float = 50_000.0,
    account_risk_pct: float = 0.005,
    max_exposure_pct: float = 0.30,
) -> RiskTargetResult:
    if df5.empty:
        return RiskTargetResult(direction, None, None, None, None, None, None,
                                ["no bar data"], ["no bar data"])
    last  = df5.iloc[-1]
    close = sf(last.get("close"))
    atr   = sf(last.get("atr14"))
    notes: list[str]    = []
    blockers: list[str] = []

    buffer      = max(atr * ATR_STOP_BUFFER_MULT, close * PCT_STOP_BUFFER, 0.02)
    noise       = _noise_floor(df5, close, atr)

    if direction == "short":
        entry = close if close < or_level else None
        if entry is None:
            blockers.append("No short entry: price not below ORL")
            return RiskTargetResult("short", None, None, None, None, None, None, notes, blockers)

        # Short stop hierarchy — QC-2: ranked priority fallback (spec: §Stop Hierarchy).
        # Prior code took max() of ALL valid anchors, which caused VWAP (often far above
        # entry) to dominate the stop calculation, systematically over-widening stops and
        # killing RR on valid setups.  The spec is explicit: hierarchy = ranked fallback,
        # #1 is "best anchor", VWAP is "last resort".  We now iterate in priority order
        # and use the FIRST valid structural anchor found, then enforce the noise floor.
        orl_band      = or_level
        vwap_sweep    = sf(last.get("vwap"))
        ema_cluster   = max(sf(last.get("ema8")), sf(last.get("ema20")))
        failed_high   = _failed_reclaim_high(df5, or_level)
        comp_ceiling  = sf(df5.tail(6)["high"].max()) if len(df5) >= 2 else None
        wick_hi       = _wick_cluster(df5, "high")

        stop: float | None = None
        best_label = "noise floor"
        for label, val in [
            ("failed reclaim high", failed_high),
            ("ORL band",            orl_band),
            ("EMA cluster",         ema_cluster),
            ("compression ceiling", comp_ceiling),
            ("wick cluster high",   wick_hi),
        ]:
            if val is None:
                continue
            fval = sf(val)
            if fval > entry:
                stop       = max(fval + buffer, entry + noise)
                best_label = label
                break                              # use first valid structural anchor

        # VWAP emergency stop — last resort when no structural anchor is above entry
        if stop is None:
            if vwap_sweep > entry:
                stop       = max(vwap_sweep + buffer, entry + noise)
                best_label = "VWAP emergency stop"
            else:
                stop       = entry + noise
                best_label = "noise floor"

        notes.append(f"Short stop anchor: {best_label}")

        t1_label, t1 = nearest_downside_wall(entry, pivots)
        _,         t2 = second_downside_wall(entry, pivots, t1)

        rr: float | None = None
        if t1 is not None and stop > entry > t1:
            rr = round((entry - t1) / (stop - entry), 2)
            if rr < RR_MIN:
                blockers.append(f"RR {rr:.2f} fails minimum {RR_MIN} after survivable stop")
        else:
            blockers.append("Invalid short geometry: no valid T1 below entry or stop ≤ entry")

        if stop - entry < noise:
            blockers.append("Stop inside noise floor — structure insufficient")

    else:  # LONG
        entry = close if close > or_level else None
        if entry is None:
            blockers.append("No long entry: price not above ORH")
            return RiskTargetResult("long", None, None, None, None, None, None, notes, blockers)

        # Long stop hierarchy — QC-3: ranked priority fallback (mirrors short fix above).
        # Prior code took min() of ALL valid anchors, which caused VWAP (often far below
        # entry) to dominate and produce unrealistically wide long stops.
        orh_support    = or_level
        vwap_bounce    = sf(last.get("vwap"))
        ema_cluster    = min(sf(last.get("ema8")), sf(last.get("ema20")))
        failed_low     = _failed_breakdown_low(df5, or_level)
        comp_floor     = sf(df5.tail(6)["low"].min()) if len(df5) >= 2 else None
        wick_lo        = _wick_cluster(df5, "low")

        stop_long: float | None = None
        best_label = "noise floor"
        for label, val in [
            ("failed breakdown low", failed_low),
            ("ORH support zone",     orh_support),
            ("EMA cluster",          ema_cluster),
            ("compression floor",    comp_floor),
            ("wick cluster low",     wick_lo),
        ]:
            if val is None:
                continue
            fval = sf(val)
            if fval < entry:
                stop_long  = min(fval - buffer, entry - noise)
                best_label = label
                break                              # use first valid structural anchor

        # VWAP bounce — last resort when no structural anchor is below entry
        if stop_long is None:
            if vwap_bounce > 0 and vwap_bounce < entry:
                stop_long  = min(vwap_bounce - buffer, entry - noise)
                best_label = "VWAP bounce (last resort)"
            else:
                stop_long  = entry - noise
                best_label = "noise floor"

        stop = stop_long
        notes.append(f"Long stop anchor: {best_label}")

        t1_label, t1 = nearest_upside_wall(entry, pivots)
        _,          t2 = second_upside_wall(entry, pivots, t1)

        rr = None
        if t1 is not None and stop < entry < t1:
            rr = round((t1 - entry) / (entry - stop), 2)
            if rr < RR_MIN:
                blockers.append(f"RR {rr:.2f} fails minimum {RR_MIN} after survivable stop")
        else:
            blockers.append("Invalid long geometry: no valid T1 above entry or stop ≥ entry")

        if entry - stop < noise:
            blockers.append("Stop inside noise floor — structure insufficient")

    # ── Position sizing (spec Step 5A) ────────────────────────────────────
    suggested_shares = 0
    try:
        stop_dist = abs(entry - stop) if (entry is not None and stop is not None) else 0
        if stop_dist > 0 and entry and entry > 0:
            risk_dollars = account_size * account_risk_pct
            shares       = int(risk_dollars / stop_dist)
            max_shares   = int((account_size * max_exposure_pct) / entry)
            suggested_shares = max(0, min(shares, max_shares))
    except Exception:
        pass

    return RiskTargetResult(
        direction=direction,
        entry=round(entry, 2) if entry is not None else None,
        stop=round(stop, 2) if stop is not None else None,   # FIX H7: removed 'stop' in dir() guard — stop is always assigned when this line is reached (early returns prevent otherwise); dir() is semantically incorrect for local-scope checks
        t1=round(t1, 2)       if t1 is not None else None,
        t2=round(t2, 2)       if t2 is not None else None,
        rr=rr,
        t1_label=t1_label,
        notes=list(dict.fromkeys(notes)),
        execution_blockers=list(dict.fromkeys(blockers)),
        suggested_shares=suggested_shares,
    )
