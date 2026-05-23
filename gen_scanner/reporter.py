"""Reporter — unified scanner v1.0.

Prints both LONG and SHORT setups in a clean, color-coded terminal layout.
Saves CSV output for logging. Both directions share the same output format.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import OUTPUT_DIR, REPORTS_DIR, TIMEZONE
from .utils import fmt_num, wrap


# ─── ANSI colour helpers ──────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

_USE_COLOR = os.name != "nt" or os.environ.get("FORCE_COLOR")


def _c(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _USE_COLOR else text


def _grade_color(grade: str) -> str:
    return {
        "A+": _BOLD + _GREEN,
        "A":  _GREEN,
        "B":  _YELLOW,
    }.get(grade, _DIM)


def _state_color(state: str) -> str:
    return {
        "TRIGGERED":          _BOLD + _GREEN,
        "READY_CONTINUATION": _GREEN,
        "WATCHLIST":          _YELLOW,
        "AVOID":              _DIM + _RED,
    }.get(state, _RESET)


def _dir_icon(direction: str) -> str:
    return "🟢 LONG" if direction == "long" else "🔴 SHORT"


# ─── Row type ─────────────────────────────────────────────────────────────

class CandidateRow(dict):
    """Dict-based row with typed accessors for convenience."""
    def g(self, key: str, default: Any = None) -> Any:
        return self.get(key, default)


# ─── Print helpers ────────────────────────────────────────────────────────

def _print_divider(char: str = "═", width: int = 88) -> None:
    print(char * width)


def _print_card(rank: int, row: CandidateRow) -> None:
    sym   = str(row.g("symbol", "")).upper()
    grade = str(row.g("grade", ""))
    state = str(row.g("state", ""))
    direc = str(row.g("direction", ""))
    win   = str(row.g("time_window", ""))
    score = row.g("score", 0)
    price = fmt_num(row.g("price"))
    entry = fmt_num(row.g("entry"))
    stop  = fmt_num(row.g("stop"))
    t1    = fmt_num(row.g("t1"))
    rr    = fmt_num(row.g("rr"))
    shrs  = row.g("suggested_shares", 0)
    et    = str(row.g("entry_type", ""))
    note  = str(row.g("note", ""))
    rejects = str(row.g("rejects", ""))
    # FIX H6: pullback_held_ema — spec-required flag shown for LONG direction
    phe   = row.g("pullback_held_ema", None)
    phe_str = ""
    if direc == "long" and phe is not None:
        phe_str = _c("✅ pullback_held_ema", _GREEN) if phe else _c("⚠ pullback_held_ema: FAIL", _RED)

    grade_str = _c(f"Grade {grade}", _grade_color(grade))
    state_str = _c(state, _state_color(state))
    dir_str   = _c(_dir_icon(direc), _CYAN)

    print(f"\n  #{rank}  {_c(sym, _BOLD)}  |  {dir_str}  |  {grade_str}  |  {state_str}")
    print(f"      Score: {score}/100  |  Window: {win}  |  EntryType: {et or '—'}")
    print(f"      Price: {price}   Entry: {entry}   Stop: {stop}   T1: {t1}   RR: {rr}   Shares: {shrs}")
    if phe_str:
        print(f"      {phe_str}")
    if note and note.lower() not in {"nan", "none", ""}:
        for line in wrap(note, 74):
            print(f"      {_c(line, _DIM)}")
    if rejects and rejects.lower() not in {"nan", "none", ""}:
        print(f"      {_c('⚠ ' + rejects[:140], _RED)}")
    print("      " + "─" * 74)


def _print_section_header(title: str) -> None:
    print(f"\n{'═' * 10}  {_c(title, _BOLD + _CYAN)}  {'═' * (68 - len(title))}")


def _print_avoid_row(row: CandidateRow) -> None:
    sym    = str(row.g("symbol", "")).upper()
    direc  = str(row.g("direction", ""))
    grade  = str(row.g("grade", "Skip"))
    rejects = str(row.g("rejects", ""))[:100]
    icon   = "🟢" if direc == "long" else "🔴"
    print(f"    {icon} {sym:<6}  {grade:<5}  {_c(rejects, _DIM)}")


# ─── Main report function ─────────────────────────────────────────────────

def report(rows: list[CandidateRow], cycle: int) -> None:
    """Print the full scanner output for one scan cycle."""
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo(TIMEZONE))
    _print_divider()
    print(_c(f"  UNIFIED 15+5 SCANNER   Cycle #{cycle}   {now_et.strftime('%H:%M:%S ET')}",
             _BOLD))
    _print_divider()

    triggered   = [r for r in rows if r.g("state") in ("TRIGGERED",)]
    ready       = [r for r in rows if r.g("state") == "READY_CONTINUATION"]
    watchlist   = [r for r in rows if r.g("state") == "WATCHLIST"]
    avoid       = [r for r in rows if r.g("state") == "AVOID" or r.g("grade") == "Skip"]

    # ── TRIGGERED ────────────────────────────────────────────────────────
    if triggered:
        _print_section_header("🔥 TRIGGERED — EXECUTE NOW")
        for i, r in enumerate(triggered, 1):
            _print_card(i, r)
    else:
        print(f"\n  {_c('No TRIGGERED setups this cycle.', _DIM)}")

    # ── READY ─────────────────────────────────────────────────────────────
    if ready:
        _print_section_header("⚡ READY — Awaiting Final Trigger")
        for i, r in enumerate(ready, 1):
            _print_card(i, r)

    # ── WATCHLIST ─────────────────────────────────────────────────────────
    if watchlist:
        _print_section_header("👁 WATCHLIST — Structurally Valid")
        for i, r in enumerate(watchlist, 1):
            _print_card(i, r)

    # ── AVOID / SKIP ───────────────────────────────────────────────────────
    if avoid:
        _print_section_header(f"🚫 FILTERED / AVOID  ({len(avoid)} names)")
        for r in avoid[:8]:
            _print_avoid_row(r)

    _print_divider("─")
    print(f"  Evaluated: {len(rows)} | "
          f"Triggered: {len(triggered)} | "
          f"Ready: {len(ready)} | "
          f"Watch: {len(watchlist)} | "
          f"Avoid: {len(avoid)}")
    _print_divider("─")


# ─── CSV logger ───────────────────────────────────────────────────────────

CSV_COLS = [
    "timestamp", "symbol", "direction", "grade", "state",
    "score", "entry_type", "time_window",
    "price", "entry", "stop", "t1", "t2", "rr", "suggested_shares",
    "orh", "orl", "vwap", "ema8", "ema20",
    "confirmers", "traps", "pullback_held_ema",   # FIX H6: spec-required flag in CSV
    "note", "rejects",
]


def write_csv(rows: list[CandidateRow]) -> Path:
    """Append rows to today's CSV output file."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(TIMEZONE))
    filename = OUTPUT_DIR / f"unified_{now.strftime('%Y%m%d')}_results.csv"

    write_header = not filename.exists()
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        ts = now.strftime("%H:%M:%S")
        for r in rows:
            row_dict = dict(r)
            row_dict["timestamp"] = ts
            writer.writerow({k: row_dict.get(k, "") for k in CSV_COLS})

    return filename


def write_trace(rows: list[CandidateRow], cycle: int) -> Path:
    """Write per-cycle decision trace to reports directory."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(TIMEZONE))
    filename = REPORTS_DIR / f"trace_{now.strftime('%Y%m%d_%H%M%S')}.txt"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"UNIFIED SCANNER — Cycle {cycle}  {now.strftime('%Y-%m-%d %H:%M:%S ET')}\n")
        f.write("=" * 88 + "\n\n")
        for r in rows:
            f.write(f"Symbol    : {r.g('symbol')}  [{r.g('direction','').upper()}]\n")
            f.write(f"Grade     : {r.g('grade')}   Score: {r.g('score')}/100\n")
            f.write(f"State     : {r.g('state')}\n")
            f.write(f"Entry Type: {r.g('entry_type','—')}\n")
            f.write(f"Price/E/S/T1: {fmt_num(r.g('price'))} / {fmt_num(r.g('entry'))} / "
                    f"{fmt_num(r.g('stop'))} / {fmt_num(r.g('t1'))}\n")
            f.write(f"RR        : {fmt_num(r.g('rr'))}\n")
            note = str(r.g("note", ""))
            if note and note not in {"nan", "none"}:
                f.write(f"Note      : {note[:200]}\n")
            rejects = str(r.g("rejects", ""))
            if rejects and rejects not in {"nan", "none"}:
                f.write(f"Rejects   : {rejects[:300]}\n")
            f.write("\n")

    return filename
