"""Main Scanner — unified 15+5 long + short scanner v1.0.

Single IBKR-compatible process that evaluates both ORH LONG and ORL SHORT
setups from one market_candidates.txt file, one data fetch per symbol.

Ticker format (market_candidates.txt):
  L:TICKER   →  long candidate (from IBKR Top % Gainers filter)
  S:TICKER   →  short candidate (from IBKR Top % Losers filter)
  TICKER     →  both directions evaluated
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent          # FIX: scanner.py is at unified_scanner/ root
sys.path.insert(0, str(ROOT))                    # add unified_scanner/ to path so gen_scanner package is importable

from gen_scanner.config import (
    SCAN_INTERVAL_SECONDS, FEEDER_ENABLED, IB_HOST, IB_PORT, IB_CLIENT_ID,
    TOP_FOCUS_COUNT, ACCOUNT_SIZE, ACCOUNT_RISK_PCT, MAX_POSITION_EXPOSURE_PCT,
    A_PLUS_ACTIVE_CAP, A_PLUS_SESSION_CAP, TIMEZONE,
)
from gen_scanner.data_feed import load_candidates, load_market_data, CandidateTicker
from gen_scanner.indicators import add_indicators, opening_range
from gen_scanner.structure_validation import validate_structure
from gen_scanner.quality_scoring import score_quality
from gen_scanner.risk_targets import calculate_risk_targets
from gen_scanner.state_machine import assign_state, evaluate_final_eligibility, current_window
from gen_scanner.reporter import CandidateRow, report, write_csv, write_trace
from gen_scanner.learning_logger import DailyAccumulator, log_setups
from gen_scanner.utils import compute_pivots, safe_float as sf


# ─── Market-hours check ───────────────────────────────────────────────────

def _is_rth() -> bool:
    now = datetime.now(ZoneInfo(TIMEZONE))
    if now.weekday() >= 5:
        return False
    s = now.replace(hour=9, minute=30, second=0, microsecond=0)
    e = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return s <= now <= e


# ─── 5m candle alignment ──────────────────────────────────────────────────

def _candle_id() -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return f"{now.date()}_{now.hour:02d}_{(now.minute // 5) * 5:02d}"


def _sleep_to_next_candle() -> None:
    now = datetime.now(ZoneInfo(TIMEZONE))
    secs_into = (now.minute % 5) * 60 + now.second + now.microsecond / 1e6
    wait = max(3.0, (300 - secs_into) + 3)
    time.sleep(min(wait, SCAN_INTERVAL_SECONDS))


# ─── Evaluate one symbol in one direction ────────────────────────────────

def _evaluate(bundle, direction: str,
              session_aplus: int, active_aplus: int) -> CandidateRow | None:
    """Run the full pipeline for one (symbol, direction) pair.

    Returns a CandidateRow or None if the symbol is outright ineligible.
    """
    sym = bundle.symbol
    try:
        df5_raw  = bundle.bars_5m
        df15_raw = bundle.bars_15m

        # Add indicators (two separate donchian windows)
        df5  = add_indicators(df5_raw, donchian_window=10)
        df15 = add_indicators(df15_raw, donchian_window=20)

        # QC-1: Inject prev_close into df5 as a column so quality_scoring.py
        # component 13 (gap quality) can read it from df5.iloc[0].get("prev_close").
        # Without this, prev_close defaults to 0 and gap quality always scores 3/8.
        if bundle.prev_close and bundle.prev_close > 0:
            df5 = df5.copy()
            df5["prev_close"] = bundle.prev_close

        if df5.empty or df15.empty:
            return None

        orh, orl = opening_range(df15)
        or_level  = orl if direction == "short" else orh

        last  = df5.iloc[-1]
        close = sf(last.get("close"))
        vwap  = sf(last.get("vwap"))
        ema8  = sf(last.get("ema8"))
        ema20 = sf(last.get("ema20"))

        # QC-6: Extract SMA200 from the last 5m bar (NaN if < 200 bars available).
        import math as _math
        sma200_val = sf(last.get("sma200", float("nan")))
        sma200_arg = sma200_val if (sma200_val > 0 and not _math.isnan(sma200_val)) else None

        # Pivot computation — QC-6: now includes PDH, PDL, SMA200 as spec-mandated walls.
        pivots = compute_pivots(
            bundle.prev_high, bundle.prev_low, bundle.prev_close, close,
            pdh=bundle.prev_high if bundle.prev_high > 0 else None,
            pdl=bundle.prev_low  if bundle.prev_low  > 0 else None,
            sma200=sma200_arg,
        )

        # Nearest wall for gate checks
        if direction == "short":
            from gen_scanner.risk_targets import nearest_downside_wall
            _, nearest_wall = nearest_downside_wall(or_level, pivots)
        else:
            from gen_scanner.risk_targets import nearest_upside_wall
            _, nearest_wall = nearest_upside_wall(or_level, pivots)

        # ── A) Structural validation (spec Category A) ────────────────────
        struct = validate_structure(
            symbol=sym,
            df5=df5, df15=df15,
            direction=direction,
            avg_volume=bundle.avg_volume,
            avg_spread_pct=bundle.avg_spread_pct,
            orh=orh, orl=orl,
            nearest_wall=nearest_wall,
            pivots=pivots,
        )

        # FIX H6: extract pullback_held_ema from confirmers_list immediately
        # after structural validation so it can be surfaced in output.
        pullback_held_ema = "pullback_held_ema" in struct.confirmers_list

        if not struct.passed:
            return CandidateRow({
                "symbol": sym, "direction": direction,
                "grade": "Skip", "state": "AVOID",
                "score": 0, "entry_type": "",
                "time_window": current_window(df5),
                "price": round(close, 2), "entry": None,
                "stop": None, "t1": None, "t2": None, "rr": None,
                "suggested_shares": 0,
                "orh": round(orh, 2), "orl": round(orl, 2),
                "vwap": round(vwap, 2), "ema8": round(ema8, 2), "ema20": round(ema20, 2),
                "confirmers": 0, "traps": struct.trap_count,
                "pullback_held_ema": False,   # FIX H6
                "note": f"{direction.upper()} — structural fail",
                "rejects": " | ".join(struct.hard_rejects[:5]),
            })

        # ── B) Quality scoring (spec Category B) ─────────────────────────
        quality = score_quality(
            df5=df5, df15=df15,
            direction=direction,
            or_level=or_level,
            nearest_wall=nearest_wall,
            entry_type=struct.entry_type,
        )

        # ── C) Preliminary state — NO RR yet (spec Category C) ───────────
        # FIX H9: preliminary state is now assigned BEFORE risk/RR calculation,
        # matching the spec's locked pipeline order (C before D).
        # FIX H5: entry_type is passed so TYPE_B flag-break trigger is detected.
        prelim = assign_state(df5=df5, direction=direction,
                              or_level=or_level, grade=quality.grade,
                              entry_type=struct.entry_type)

        # QC-7: pullback_held_ema hard gate for LONG TRIGGERED state.
        # The LONG spec (Step 4A, "Pullback Depth Check at Entry") is explicit:
        #   "Fail — Do Not Enter: Retest candle closed below EMA 20."
        #   "If flag is degraded or missing — wait for the next clean setup."
        # This is a REQUIRED entry check, not an optional quality bonus.
        # If pullback_held_ema is False on a TRIGGERED LONG, downgrade to
        # READY_CONTINUATION — the retest structure is broken.
        if (direction == "long"
                and prelim.state == "TRIGGERED"
                and not pullback_held_ema):
            prelim = type(prelim)(
                state="READY_CONTINUATION",
                reason="pullback_held_ema FAIL — retest closed below EMA20; wait for valid retest",
            )

        # ── D) Survivable stop + T1 + RR recalculation (spec Category D) ─
        risk = calculate_risk_targets(
            df5=df5,
            direction=direction,
            or_level=or_level,
            pivots=pivots,
            account_size=ACCOUNT_SIZE,
            account_risk_pct=ACCOUNT_RISK_PCT,
            max_exposure_pct=MAX_POSITION_EXPOSURE_PCT,
        )

        # ── E) Final execution eligibility (spec Category E) ─────────────
        final = evaluate_final_eligibility(
            df5=df5, df15=df15,
            direction=direction,
            or_level=or_level,
            prelim_state=prelim.state,
            grade=quality.grade,
            risk=risk,
        )

        effective_grade = final.grade_override or quality.grade
        final_state     = final.final_state

        # ── A+ scarcity enforcement ───────────────────────────────────────
        if effective_grade == "A+" and (
            active_aplus >= A_PLUS_ACTIVE_CAP or
            session_aplus >= A_PLUS_SESSION_CAP
        ):
            effective_grade = "A"

        # ── Build note string ─────────────────────────────────────────────
        note_parts = []
        if quality.notes:
            note_parts.extend(quality.notes[:3])
        if final.notes:
            note_parts.extend([n for n in final.notes if "clear" not in n.lower()])

        rejects_parts = []
        if risk.execution_blockers:
            rejects_parts.extend(risk.execution_blockers[:2])
        if final.blockers:
            rejects_parts.extend([b for b in final.blockers if b not in rejects_parts][:2])

        return CandidateRow({
            "symbol":          sym,
            "direction":       direction,
            "grade":           effective_grade,
            "state":           final_state,
            "score":           quality.score,
            "entry_type":      struct.entry_type,
            "time_window":     current_window(df5),
            "price":           round(close, 2),
            "entry":           risk.entry,
            "stop":            risk.stop,
            "t1":              risk.t1,
            "t2":              risk.t2,
            "rr":              risk.rr,
            "suggested_shares": risk.suggested_shares,
            "orh":             round(orh, 2),
            "orl":             round(orl, 2),
            "vwap":            round(vwap, 2),
            "ema8":            round(ema8, 2),
            "ema20":           round(ema20, 2),
            "confirmers":      struct.confirmers_count,
            "traps":           struct.trap_count,
            "pullback_held_ema": pullback_held_ema,   # FIX H6: spec-required flag
            "note":            " | ".join(note_parts),
            "rejects":         " | ".join(rejects_parts),
        })

    except Exception as exc:
        print(f"  [SKIP] {sym} ({direction}): {exc}")
        return None


# ─── Main scan cycle ──────────────────────────────────────────────────────

def scan_once(cycle: int,
              session_aplus: int) -> tuple[list[CandidateRow], int]:
    """Run one full scan cycle. Returns (rows, new_session_aplus_count)."""
    candidates = load_candidates()
    if not candidates:
        print("[SCAN] market_candidates.txt is empty. Add tickers (L:AAPL / S:MSFT).")
        return [], session_aplus

    print(f"\n  Candidates: {', '.join(f'{c.direction[:1].upper()}:{c.symbol}' for c in candidates)}")

    rows: list[CandidateRow] = []
    active_aplus = 0

    for candidate in candidates:
        # Load market data once per symbol
        bundle = load_market_data(candidate)
        if bundle is None:
            continue

        # Determine which directions to evaluate
        directions = []
        if candidate.direction in ("short", "both"):
            directions.append("short")
        if candidate.direction in ("long", "both"):
            directions.append("long")

        for d in directions:
            row = _evaluate(bundle, d, session_aplus, active_aplus)
            if row is None:
                continue
            if row.g("grade") == "A+":
                active_aplus += 1
                session_aplus += 1
            rows.append(row)

    # Sort: triggered first, then by score descending
    state_order = {"TRIGGERED": 0, "READY_CONTINUATION": 1, "WATCHLIST": 2, "AVOID": 3}
    rows.sort(key=lambda r: (state_order.get(r.g("state", ""), 4), -(r.g("score") or 0)))

    return rows, session_aplus


# ─── Entry point ─────────────────────────────────────────────────────────

def run_forever() -> None:
    print("\n" + "═" * 88)
    print("  UNIFIED 15+5 SCANNER  — Long (ORH) + Short (ORL) — v1.0")
    print("  Feed mode: MANUAL — edit gen_scanner/config/market_candidates.txt")
    print("  Ticker format: L:AAPL (long only), S:MSFT (short only), TSLA (both)")
    print("  Scan cadence: 60s, aligned to closed 5m candle")
    print("  Press Ctrl+C to stop.")
    print("═" * 88 + "\n")

    session_aplus = 0
    last_candle   = ""
    last_session_date: date | None = None
    cycle = 0
    accumulator = DailyAccumulator()

    try:
        while True:
            # Session reset on new trading day
            today = datetime.now(ZoneInfo(TIMEZONE)).date()
            if last_session_date != today:
                session_aplus = 0
                last_candle   = ""
                last_session_date = today
                print(f"\n[SESSION] New day {today} — counters reset.")

            current_candle = _candle_id()
            if current_candle == last_candle:
                # Heartbeat between candle closes
                now_str = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M:%S")
                print(f"  [{now_str} ET] ⏱ Waiting for next 5m candle close...", end="\r")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            last_candle = current_candle
            cycle += 1

            print("\n" + "=" * 88)
            print(f"[SCAN CYCLE {cycle}]  {datetime.now(ZoneInfo(TIMEZONE)).strftime('%H:%M ET')}")
            print("=" * 88)

            try:
                rows, session_aplus = scan_once(cycle, session_aplus)

                if _is_rth():
                    report(rows, cycle)
                else:
                    print("\n⚠️  MARKET CLOSED — Analysis mode (no live trades)")
                    report(rows, cycle)

                if rows:
                    write_csv(rows)
                    write_trace(rows, cycle)
                    log_setups([dict(r) for r in rows], cycle)
                    accumulator.ingest([dict(r) for r in rows], cycle)

            except Exception as exc:
                import traceback
                print(f"[SCAN ERROR] {exc}")
                traceback.print_exc()

            print(f"\n[Cycle {cycle} complete — next scan in ~{SCAN_INTERVAL_SECONDS}s]")
            _sleep_to_next_candle()

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Unified scanner terminated by user.")
        accumulator.finalize()
        print("[LEARNING] Run post_market.bat for daily review + dashboard.")


if __name__ == "__main__":
    run_forever()
