"""Shared utilities — unified scanner v1.0."""
from __future__ import annotations
import pandas as pd
import numpy as np


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return float(value)
    except Exception:
        return default


def slope(series: pd.Series, lookback: int = 3) -> float:
    """Linear slope of the last `lookback` values."""
    try:
        tail = series.dropna().tail(lookback)
        if len(tail) < 2:
            return 0.0
        x = np.arange(len(tail), dtype=float)
        y = tail.astype(float).values
        m = np.polyfit(x, y, 1)[0]
        return float(m)
    except Exception:
        return 0.0


def ema_actively_separating(
    df: pd.DataFrame, lookback: int = 4, direction: str = "bearish"
) -> bool:
    """True when EMA 8 and EMA 20 are actively widening in the expected direction.

    direction='bearish'  → EMA8 < EMA20 and gap is growing (short bias)
    direction='bullish'  → EMA8 > EMA20 and gap is growing (long bias)
    """
    if "ema8" not in df.columns or "ema20" not in df.columns:
        return False
    tail = df.tail(lookback + 1)
    if len(tail) < 3:
        return False
    if direction == "bearish":
        gaps = (tail["ema20"].astype(float) - tail["ema8"].astype(float)).tolist()
        return gaps[-1] > 0 and gaps[-1] > gaps[0]
    else:  # bullish
        gaps = (tail["ema8"].astype(float) - tail["ema20"].astype(float)).tolist()
        return gaps[-1] > 0 and gaps[-1] > gaps[0]


def gap_down_pct(df5: pd.DataFrame) -> float:
    """Return the opening gap-down percent (positive = gap down, negative = gap up)."""
    try:
        today_open = safe_float(df5.iloc[0].get("open"))
        prev_close = safe_float(df5.iloc[0].get("prev_close", 0))
        if prev_close <= 0:
            return 0.0
        return (prev_close - today_open) / prev_close
    except Exception:
        return 0.0


def gap_up_pct(df5: pd.DataFrame) -> float:
    """Return the opening gap-up percent (positive = gap up)."""
    return -gap_down_pct(df5)


def compute_pivots(prev_high: float, prev_low: float, prev_close: float,
                   current_price: float,
                   pdh: float | None = None,
                   pdl: float | None = None,
                   sma200: float | None = None) -> dict[str, float]:
    """Floor-trader pivot formula. Returns levels keyed by label.

    QC-4: Extended with PDH (Prior Day High), PDL (Prior Day Low), and SMA200.
    Both specs list these as tier-1 key walls for resistance (PDH/SMA200) and
    support (PDL/SMA200). Passing None for any optional level simply omits it.
    """
    if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
        return {}
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2 * pp - prev_low
    r2 = pp + (prev_high - prev_low)
    s1 = 2 * pp - prev_high
    s2 = pp - (prev_high - prev_low)
    s3 = prev_low - 2 * (prev_high - pp)

    # Nearest whole-dollar levels
    price = current_price
    round1 = float(int(price))          # floor whole dollar
    round2 = round1 - 1.0
    round_up1 = round1 + 1.0
    round_up2 = round1 + 2.0

    result: dict[str, float] = {
        "PP": round(pp, 2),
        "R1": round(r1, 2),
        "R2": round(r2, 2),
        "S1": round(s1, 2),
        "S2": round(s2, 2),
        "S3": round(s3, 2),
        "ROUND_DN1": round(round1, 2),
        "ROUND_DN2": round(round2, 2),
        "ROUND_UP1": round(round_up1, 2),
        "ROUND_UP2": round(round_up2, 2),
    }

    # QC-4: PDH / PDL — prior day high and low.
    # These are spec-mandated tier-1 walls.  We include them only when prev_high/
    # prev_low are valid (same guard as the pivot formula above).
    if pdh and pdh > 0:
        result["PDH"] = round(pdh, 2)
    if pdl and pdl > 0:
        result["PDL"] = round(pdl, 2)

    # QC-4: SMA200 — included when ≥200 bars available (NaN otherwise, not added).
    import math
    if sma200 is not None and not math.isnan(sma200) and sma200 > 0:
        result["SMA200"] = round(sma200, 2)

    return result


def fmt_num(value, decimals: int = 2) -> str:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return "—"
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "—"


def wrap(text: str, width: int = 74) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for w in words:
        if len(current) + len(w) + 1 > width:
            if current:
                lines.append(current)
            current = w
        else:
            current = f"{current} {w}".strip()
    if current:
        lines.append(current)
    return lines
