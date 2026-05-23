"""post_market.py — Automatic post-market data collector & daily analyzer.

Run this after market close (or schedule it).
It:
  1. Finalizes today's daily_stats.csv row
  2. Analyzes setups_log.csv for patterns
  3. Prints a full terminal review of the day
  4. Calls generate_dashboard.py to build/update the HTML dashboard

Usage:
  python post_market.py
  (or double-click post_market.bat)
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gen_scanner.config import LEARNING_DIR, TIMEZONE
from gen_scanner.learning_logger import (
    SETUPS_LOG, DAILY_STATS, OUTCOMES_LOG, PERFORMANCE_LOG,
    _init_all,
)


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _today() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "", "nan") else default
    except Exception:
        return default


def _bar(value: float, max_val: float, width: int = 30, fill: str = "█") -> str:
    filled = int((value / max(max_val, 1)) * width)
    return fill * filled + "░" * (width - filled)


def run_daily_review() -> None:
    _init_all()
    today = _today()
    print("\n" + "═" * 72)
    print(f"  📊 POST-MARKET DAILY REVIEW  —  {today}")
    print("═" * 72)

    # ── 1. Today's setups ────────────────────────────────────────────────
    all_setups = _load_csv(SETUPS_LOG)
    today_setups = [r for r in all_setups if r.get("date") == today]

    if not today_setups:
        print("\n  No scan data found for today.")
        print("  Did the scanner run? Check setups_log.csv in the learning/ folder.")
    else:
        # Deduplicate by symbol+direction+state (keep highest score per pair)
        best: dict[str, dict] = {}
        for r in today_setups:
            key = f"{r['symbol']}|{r['direction']}"
            if key not in best or _safe_float(r.get("score")) > _safe_float(best[key].get("score")):
                best[key] = r
        unique = list(best.values())

        # Grade counts
        grades = defaultdict(int)
        states = defaultdict(int)
        dirs   = defaultdict(int)
        for r in unique:
            grades[r.get("grade","?")] += 1
            states[r.get("state","?")] += 1
            dirs[r.get("direction","?")] += 1

        total_cycles = len(set(r.get("cycle","") for r in today_setups))

        print(f"\n  SCAN SUMMARY")
        print(f"  {'─'*50}")
        print(f"  Total scan cycles today : {total_cycles}")
        print(f"  Unique setups evaluated : {len(unique)}")
        print(f"  Longs evaluated         : {dirs.get('long', 0)}")
        print(f"  Shorts evaluated        : {dirs.get('short', 0)}")

        print(f"\n  GRADE BREAKDOWN")
        print(f"  {'─'*50}")
        for g, label in [("A+","Elite  "), ("A","Strong "), ("B","Watch  "), ("Skip","Skip   ")]:
            cnt = grades.get(g, 0)
            print(f"  {label} {g:<4}  {cnt:>3}  {_bar(cnt, max(grades.values(), default=1), 25)}")

        print(f"\n  STATE BREAKDOWN")
        print(f"  {'─'*50}")
        state_map = [
            ("TRIGGERED",           "🔥 Triggered   "),
            ("READY_CONTINUATION",  "⚡ Ready        "),
            ("WATCHLIST",           "👁 Watchlist    "),
            ("AVOID",               "🚫 Avoid        "),
        ]
        for s, label in state_map:
            cnt = states.get(s, 0)
            print(f"  {label}  {cnt:>3}  {_bar(cnt, max(states.values(), default=1), 20)}")

        # Top triggered + ready
        top = [r for r in unique if r.get("state") in ("TRIGGERED", "READY_CONTINUATION")]
        top.sort(key=lambda r: (_safe_float(r.get("score")), _safe_float(r.get("rr"))), reverse=True)

        if top:
            print(f"\n  TOP SETUPS TODAY")
            print(f"  {'─'*68}")
            print(f"  {'#':<3} {'SYM':<8} {'DIR':<6} {'GRD':<4} {'STATE':<22} {'SCORE':<7} {'RR':<5} {'ENTRY TYPE'}")
            print(f"  {'─'*68}")
            for i, r in enumerate(top[:10], 1):
                icon = "🟢" if r.get("direction") == "long" else "🔴"
                print(f"  {i:<3} {icon}{r.get('symbol','?'):<7} {r.get('direction','?'):<6} "
                      f"{r.get('grade','?'):<4} {r.get('state','?'):<22} "
                      f"{r.get('score','?'):<7} {r.get('rr','—'):<5} {r.get('entry_type','—')}")

    # ── 2. Outcomes ───────────────────────────────────────────────────────
    outcomes = _load_csv(OUTCOMES_LOG)
    today_outcomes = [r for r in outcomes if r.get("date") == today]

    print(f"\n  TODAY'S TRADE OUTCOMES")
    print(f"  {'─'*68}")
    if not today_outcomes:
        print("  No outcomes logged yet.")
        print("  Run log_outcome.bat after each trading session to record results.")
    else:
        wins      = [r for r in today_outcomes if r.get("result") == "WIN"]
        losses    = [r for r in today_outcomes if r.get("result") == "LOSS"]
        scratches = [r for r in today_outcomes if r.get("result") == "SCRATCH"]
        missed    = [r for r in today_outcomes if r.get("result") == "MISSED"]
        total     = len([r for r in today_outcomes if r.get("result") in ("WIN","LOSS","SCRATCH")])
        win_rate  = round(len(wins) / total * 100, 1) if total > 0 else 0.0
        net_r     = sum(_safe_float(r.get("actual_pnl_r")) for r in today_outcomes
                        if r.get("result") in ("WIN","LOSS","SCRATCH"))

        print(f"  Trades taken   : {total}")
        print(f"  Wins           : {len(wins)}")
        print(f"  Losses         : {len(losses)}")
        print(f"  Scratches      : {len(scratches)}")
        print(f"  Missed signals : {len(missed)}")
        print(f"  Win rate       : {win_rate}%")
        print(f"  Net R today    : {net_r:+.2f}R")

        for r in today_outcomes:
            icon = {"WIN":"✅","LOSS":"❌","SCRATCH":"↔️","MISSED":"👁"}.get(r.get("result",""),"·")
            dir_icon = "🟢" if r.get("direction") == "long" else "🔴"
            pnl = r.get("actual_pnl_r","")
            pnl_str = f"{float(pnl):+.2f}R" if pnl else "—"
            print(f"    {icon} {dir_icon} {r.get('symbol','?'):<6} {r.get('direction','?'):<6} "
                  f"Entry: {r.get('entry','?')}  Exit: {r.get('actual_exit','?')}  "
                  f"P&L: {pnl_str}  [{r.get('result','')}]")

    # ── 3. Historical performance ─────────────────────────────────────────
    perf_all = _load_csv(PERFORMANCE_LOG)
    if perf_all:
        all_trades  = sum(_safe_float(r.get("total_trades")) for r in perf_all)
        all_wins    = sum(_safe_float(r.get("wins")) for r in perf_all)
        net_r_all   = sum(_safe_float(r.get("net_r")) for r in perf_all)
        wr_all      = round(all_wins / all_trades * 100, 1) if all_trades > 0 else 0.0

        print(f"\n  ALL-TIME PERFORMANCE SUMMARY")
        print(f"  {'─'*50}")
        print(f"  Total trades   : {int(all_trades)}")
        print(f"  Overall win %  : {wr_all}%")
        print(f"  Net R (all)    : {net_r_all:+.2f}R")

        # Last 5 days
        if len(perf_all) >= 2:
            print(f"\n  LAST {min(5,len(perf_all))} TRADING DAYS")
            print(f"  {'DATE':<12} {'TRADES':<8} {'WIN%':<7} {'NET R':<8} {'BAR'}")
            print(f"  {'─'*55}")
            for r in perf_all[-5:]:
                net  = _safe_float(r.get("net_r"))
                wr   = _safe_float(r.get("win_rate_pct"))
                bar  = _bar(max(net, 0), 3.0, 15, "█") if net >= 0 else _bar(abs(net), 3.0, 15, "▒")
                sign = "+" if net >= 0 else ""
                print(f"  {r.get('date','?'):<12} {r.get('total_trades','0'):<8} "
                      f"{wr:<6.1f}%  {sign}{net:<7.2f}  {bar}")

    # ── 4. Write today to daily_stats.csv so dashboard KPIs are always current ──
    if today_setups:
        from gen_scanner.learning_logger import DAILY_STATS, DAILY_COLS
        import csv as _csv
        best = {}
        for r in today_setups:
            key = f"{r.get('symbol')}|{r.get('direction')}"
            if key not in best or _safe_float(r.get("score")) > _safe_float(best[key].get("score")):
                best[key] = r
        uniq = list(best.values())
        sc, gc = defaultdict(int), defaultdict(int)
        for r in uniq:
            sc[r.get("state","AVOID")] += 1
            gc[r.get("grade","Skip")]  += 1
        br, bs, bd = 0.0, "", ""
        for r in uniq:
            if _safe_float(r.get("rr")) > br:
                br, bs, bd = _safe_float(r.get("rr")), r.get("symbol",""), r.get("direction","")
        times = sorted(r.get("time","") for r in today_setups if r.get("time"))
        record = {
            "date": today,
            "total_cycles": len(set(r.get("cycle","") for r in today_setups)),
            "total_symbols_evaluated": len(uniq),
            "total_triggered":  sc.get("TRIGGERED", 0),
            "total_ready":      sc.get("READY_CONTINUATION", 0),
            "total_watchlist":  sc.get("WATCHLIST", 0),
            "total_avoid":      sc.get("AVOID", 0),
            "aplus_count": gc.get("A+",0), "a_count": gc.get("A",0),
            "b_count": gc.get("B",0), "skip_count": gc.get("Skip",0),
            "long_triggered":  sum(1 for r in uniq if r.get("direction")=="long"  and r.get("state")=="TRIGGERED"),
            "short_triggered": sum(1 for r in uniq if r.get("direction")=="short" and r.get("state")=="TRIGGERED"),
            "long_ready":      sum(1 for r in uniq if r.get("direction")=="long"  and r.get("state")=="READY_CONTINUATION"),
            "short_ready":     sum(1 for r in uniq if r.get("direction")=="short" and r.get("state")=="READY_CONTINUATION"),
            "best_rr_setup": round(br,2) if br else "",
            "best_rr_symbol": bs, "best_rr_direction": bd,
            "session_start": times[0][:5] if times else "",
            "session_end":   times[-1][:5] if times else "",
        }
        existing = []
        if DAILY_STATS.exists():
            with open(DAILY_STATS,"r",encoding="utf-8") as f:
                existing = [r for r in _csv.DictReader(f) if r.get("date") != today]
        with open(DAILY_STATS,"w",newline="",encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=DAILY_COLS)
            w.writeheader(); w.writerows(existing); w.writerow(record)
        print(f"  [POST-MARKET] Daily stats saved to daily_stats.csv ✅")

    print(f"\n  ─────────────────────────────────────────────────────────────────")
    print(f"  💡 To log today's trade outcomes: run  log_outcome.bat")
    print(f"  📊 To open the HTML dashboard:   run  generate_dashboard.bat")
    print("═" * 72 + "\n")


if __name__ == "__main__":
    run_daily_review()
