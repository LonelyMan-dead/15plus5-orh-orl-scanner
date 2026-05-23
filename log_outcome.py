"""log_outcome.py — Interactive trade outcome logger.

Run after market close to log trade results. Reads today's triggered/ready
setups from setups_log.csv, asks you to fill in actual exit + P&L,
then saves to outcomes_log.csv and recalculates performance stats.

Usage:
  python log_outcome.py
  (or double-click log_outcome.bat on Windows)
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gen_scanner.config import LEARNING_DIR, TIMEZONE
from gen_scanner.learning_logger import (
    SETUPS_LOG, OUTCOMES_LOG, PERFORMANCE_LOG,
    OUTCOME_COLS, PERFORMANCE_COLS,
    _ensure, _init_all,
)


def _load_today_setups() -> list[dict]:
    """Load today's TRIGGERED + READY setups from setups_log.csv."""
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    if not SETUPS_LOG.exists():
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    with open(SETUPS_LOG, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") != today:
                continue
            if row.get("state") not in ("TRIGGERED", "READY_CONTINUATION"):
                continue
            key = f"{row['symbol']}|{row['direction']}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _load_existing_outcomes() -> list[dict]:
    if not OUTCOMES_LOG.exists():
        return []
    with open(OUTCOMES_LOG, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _already_logged(symbol: str, direction: str, today: str,
                    existing: list[dict]) -> bool:
    for r in existing:
        if (r.get("symbol") == symbol and
                r.get("direction") == direction and
                r.get("date") == today):
            return True
    return False


def _recalculate_performance() -> None:
    """Rebuild performance_log.csv from outcomes_log.csv."""
    if not OUTCOMES_LOG.exists():
        return
    with open(OUTCOMES_LOG, "r", encoding="utf-8") as f:
        all_outcomes = list(csv.DictReader(f))

    by_date: dict[str, list[dict]] = {}
    for r in all_outcomes:
        d = r.get("date", "")
        by_date.setdefault(d, []).append(r)

    records: list[dict] = []
    for d, rows in sorted(by_date.items()):
        trades = [r for r in rows if r.get("result") in ("WIN", "LOSS", "SCRATCH")]
        wins   = [r for r in trades if r.get("result") == "WIN"]
        losses = [r for r in trades if r.get("result") == "LOSS"]

        def safe_r(r: dict) -> float:
            try:
                return float(r.get("actual_pnl_r", 0))
            except Exception:
                return 0.0

        win_rs  = [safe_r(r) for r in wins]
        loss_rs = [safe_r(r) for r in losses]
        all_rs  = [safe_r(r) for r in trades]

        records.append({
            "date":           d,
            "total_trades":   len(trades),
            "wins":           len(wins),
            "losses":         len(losses),
            "scratches":      len([r for r in trades if r.get("result") == "SCRATCH"]),
            "win_rate_pct":   round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_r_winner":   round(sum(win_rs) / len(win_rs), 2) if win_rs else 0,
            "avg_r_loser":    round(sum(loss_rs) / len(loss_rs), 2) if loss_rs else 0,
            "total_r_gained": round(sum(win_rs), 2),
            "total_r_lost":   round(sum(loss_rs), 2),
            "net_r":          round(sum(all_rs), 2),
            "best_trade_r":   round(max(all_rs), 2) if all_rs else 0,
            "worst_trade_r":  round(min(all_rs), 2) if all_rs else 0,
        })

    with open(PERFORMANCE_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PERFORMANCE_COLS)
        writer.writeheader()
        writer.writerows(records)

    print(f"\n  ✅ Performance log updated: {PERFORMANCE_LOG.name}")


def _prompt(label: str, options: list[str] | None = None,
            default: str = "") -> str:
    opts = f" [{'/'.join(options)}]" if options else ""
    dflt = f" (default: {default})" if default else ""
    while True:
        val = input(f"  {label}{opts}{dflt}: ").strip()
        if not val and default:
            return default
        if options and val.upper() not in [o.upper() for o in options]:
            print(f"    Please enter one of: {', '.join(options)}")
            continue
        return val.upper() if options else val


def main() -> None:
    _init_all()
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    print("\n" + "═" * 64)
    print(f"  TRADE OUTCOME LOGGER  —  {today}")
    print("  Enter results for today's triggered/ready setups.")
    print("═" * 64)

    setups   = _load_today_setups()
    existing = _load_existing_outcomes()

    if not setups:
        print("\n  No triggered/ready setups found for today.")
        print("  (setups_log.csv is empty or scanner hasn't run today)")
        ans = input("\n  Log a manual trade entry? [Y/N]: ").strip().upper()
        if ans != "Y":
            return
        setups = [{"symbol": "", "direction": "", "grade": "",
                   "state": "", "entry": "", "stop": "", "t1": "", "rr": ""}]

    new_outcomes: list[dict] = []

    for setup in setups:
        sym   = setup.get("symbol", "")
        direc = setup.get("direction", "")
        grade = setup.get("grade", "")
        state = setup.get("state", "")
        entry = setup.get("entry", "")
        stop  = setup.get("stop", "")
        t1    = setup.get("t1", "")
        rr    = setup.get("rr", "")

        if sym and _already_logged(sym, direc, today, existing + new_outcomes):
            print(f"\n  ⏭  {sym} ({direc.upper()}) — already logged today, skipping.")
            continue

        icon = "🟢" if direc == "long" else "🔴"
        print(f"\n  {icon} {sym or '?'}  {direc.upper()}  Grade: {grade}  State: {state}")
        print(f"     Entry: {entry}  Stop: {stop}  T1: {t1}  RR: {rr}")

        if not sym:
            sym   = input("  Symbol: ").strip().upper()
            direc = _prompt("Direction", ["LONG", "SHORT"]).lower()
            entry = input("  Entry price: ").strip()
            stop  = input("  Stop price: ").strip()
            t1    = input("  T1 target: ").strip()
            rr    = input("  Planned RR: ").strip()

        result = _prompt("Did you take this trade? Result",
                         ["WIN", "LOSS", "SCRATCH", "MISSED", "SKIP"])
        if result == "SKIP":
            continue

        actual_exit = ""
        actual_pnl  = ""
        if result in ("WIN", "LOSS", "SCRATCH"):
            actual_exit = input("  Actual exit price: ").strip()
            actual_pnl  = input("  P&L in R (e.g. 1.8 for +1.8R, -1.0 for -1R): ").strip()
        notes = input("  Notes (optional, press Enter to skip): ").strip()

        new_outcomes.append({
            "date":        today,
            "symbol":      sym,
            "direction":   direc,
            "grade":       grade,
            "state":       state,
            "entry":       entry,
            "stop":        stop,
            "t1":          t1,
            "rr_planned":  rr,
            "actual_exit": actual_exit,
            "actual_pnl_r": actual_pnl,
            "result":      result,
            "notes":       notes,
            "entered_at":  datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M:%S"),
        })

    if new_outcomes:
        with open(OUTCOMES_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTCOME_COLS, extrasaction="ignore")
            writer.writerows(new_outcomes)
        print(f"\n  ✅ {len(new_outcomes)} outcome(s) saved to outcomes_log.csv")
        _recalculate_performance()
    else:
        print("\n  No new outcomes logged.")

    print("\n  Run generate_dashboard.bat to view your updated dashboard.")
    print("═" * 64 + "\n")


if __name__ == "__main__":
    main()
