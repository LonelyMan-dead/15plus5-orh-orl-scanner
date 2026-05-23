"""Learning Logger — unified scanner v1.0.

Automatically logs every evaluated setup to CSV files each scan cycle.
These CSVs feed:
  - post_market.py (daily review & stats)
  - dashboard_generator.py (HTML dashboard)
  - outcome tracking (log_outcome.py)

Files created in learning/:
  setups_log.csv         — every evaluated symbol each cycle
  daily_stats.csv        — per-day aggregate stats (appended after close)
  outcomes_log.csv       — trade outcomes entered by user via log_outcome.py
  performance_log.csv    — running win rate / P&L summary
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import LEARNING_DIR, TIMEZONE

SETUPS_LOG     = LEARNING_DIR / "setups_log.csv"
DAILY_STATS    = LEARNING_DIR / "daily_stats.csv"
OUTCOMES_LOG   = LEARNING_DIR / "outcomes_log.csv"
PERFORMANCE_LOG = LEARNING_DIR / "performance_log.csv"

SETUP_COLS = [
    "date", "time", "cycle",
    "symbol", "direction", "grade", "state",
    "score", "entry_type", "time_window",
    "price", "entry", "stop", "t1", "rr",
    "orh", "orl", "vwap", "ema8", "ema20",
    "confirmers", "traps", "pullback_held_ema",   # QC-10: spec-required flag added
    "note", "rejects",
]

DAILY_COLS = [
    "date",
    "total_cycles", "total_symbols_evaluated",
    "total_triggered", "total_ready", "total_watchlist", "total_avoid",
    "aplus_count", "a_count", "b_count", "skip_count",
    "long_triggered", "short_triggered",
    "long_ready", "short_ready",
    "best_rr_setup", "best_rr_symbol", "best_rr_direction",
    "session_start", "session_end",
]

OUTCOME_COLS = [
    "date", "symbol", "direction", "grade", "state",
    "entry", "stop", "t1", "rr_planned",
    "actual_exit", "actual_pnl_r", "result",  # result: WIN / LOSS / SCRATCH / MISSED
    "notes",
    "entered_at",
]

PERFORMANCE_COLS = [
    "date", "total_trades", "wins", "losses", "scratches",
    "win_rate_pct", "avg_r_winner", "avg_r_loser",
    "total_r_gained", "total_r_lost", "net_r",
    "best_trade_r", "worst_trade_r",
]


def _ensure(path: Path, cols: list[str]) -> None:
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=cols).writeheader()


def _init_all() -> None:
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    _ensure(SETUPS_LOG,      SETUP_COLS)
    _ensure(DAILY_STATS,     DAILY_COLS)
    _ensure(OUTCOMES_LOG,    OUTCOME_COLS)
    _ensure(PERFORMANCE_LOG, PERFORMANCE_COLS)


# ─── Setup logger ─────────────────────────────────────────────────────────

def log_setups(rows: list[dict], cycle: int) -> None:
    """Append all evaluated rows from one scan cycle to setups_log.csv."""
    _init_all()
    now = datetime.now(ZoneInfo(TIMEZONE))
    with open(SETUPS_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SETUP_COLS, extrasaction="ignore")
        for row in rows:
            record = dict(row)
            record["date"]  = now.strftime("%Y-%m-%d")
            record["time"]  = now.strftime("%H:%M:%S")
            record["cycle"] = cycle
            writer.writerow({k: record.get(k, "") for k in SETUP_COLS})


# ─── Daily stats accumulator ──────────────────────────────────────────────

class DailyAccumulator:
    """Tracks intra-day stats across all cycles. Call .finalize() at session end."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.session_date: date | None = None
        self.cycles   = 0
        self.symbols  = 0
        self.triggered = 0
        self.ready     = 0
        self.watchlist = 0
        self.avoid     = 0
        self.aplus = self.a = self.b = self.skip = 0
        self.long_triggered = self.short_triggered = 0
        self.long_ready     = self.short_ready     = 0
        self.best_rr: float | None = None
        self.best_rr_sym  = ""
        self.best_rr_dir  = ""
        self.session_start = ""
        self.session_end   = ""

    def ingest(self, rows: list[dict], cycle: int) -> None:
        now = datetime.now(ZoneInfo(TIMEZONE))
        if self.session_date is None:
            self.session_date = now.date()
            self.session_start = now.strftime("%H:%M")
        self.session_end = now.strftime("%H:%M")
        self.cycles += 1
        self.symbols += len(rows)

        for r in rows:
            state = str(r.get("state", ""))
            grade = str(r.get("grade", ""))
            direc = str(r.get("direction", ""))
            rr    = r.get("rr")

            if state == "TRIGGERED":     self.triggered += 1
            elif state == "READY_CONTINUATION": self.ready += 1
            elif state == "WATCHLIST":   self.watchlist += 1
            else:                        self.avoid += 1

            if grade == "A+":  self.aplus += 1
            elif grade == "A": self.a += 1
            elif grade == "B": self.b += 1
            else:              self.skip += 1

            if state == "TRIGGERED" and direc == "long":   self.long_triggered += 1
            if state == "TRIGGERED" and direc == "short":  self.short_triggered += 1
            if state == "READY_CONTINUATION" and direc == "long":  self.long_ready += 1
            if state == "READY_CONTINUATION" and direc == "short": self.short_ready += 1

            try:
                rr_f = float(rr) if rr else 0.0
                if rr_f > (self.best_rr or 0.0):
                    self.best_rr     = rr_f
                    self.best_rr_sym = str(r.get("symbol", ""))
                    self.best_rr_dir = direc
            except Exception:
                pass

    def finalize(self) -> None:
        """Write today's stats to daily_stats.csv."""
        _init_all()
        today = (self.session_date or datetime.now(ZoneInfo(TIMEZONE)).date()).strftime("%Y-%m-%d")
        record = {
            "date": today,
            "total_cycles": self.cycles,
            "total_symbols_evaluated": self.symbols,
            "total_triggered": self.triggered,
            "total_ready":     self.ready,
            "total_watchlist": self.watchlist,
            "total_avoid":     self.avoid,
            "aplus_count":     self.aplus,
            "a_count":         self.a,
            "b_count":         self.b,
            "skip_count":      self.skip,
            "long_triggered":  self.long_triggered,
            "short_triggered": self.short_triggered,
            "long_ready":      self.long_ready,
            "short_ready":     self.short_ready,
            "best_rr_setup":   round(self.best_rr, 2) if self.best_rr else "",
            "best_rr_symbol":  self.best_rr_sym,
            "best_rr_direction": self.best_rr_dir,
            "session_start":   self.session_start,
            "session_end":     self.session_end,
        }
        # Overwrite today's row (remove existing if present)
        existing: list[dict] = []
        if DAILY_STATS.exists():
            with open(DAILY_STATS, "r", encoding="utf-8") as f:
                existing = [r for r in csv.DictReader(f) if r.get("date") != today]
        with open(DAILY_STATS, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DAILY_COLS)
            writer.writeheader()
            writer.writerows(existing)
            writer.writerow(record)
        print(f"\n[LEARNING] Day stats saved → {DAILY_STATS.name}")
        print(f"  Cycles: {self.cycles} | Triggered: {self.triggered} | "
              f"Ready: {self.ready} | A+: {self.aplus}")
