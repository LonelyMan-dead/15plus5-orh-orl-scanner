"""generate_dashboard.py — HTML dashboard generator.

Reads all CSV files from learning/ and generates a beautiful
standalone HTML dashboard at dashboard/daily_dashboard.html.

Run after market close or after logging outcomes.
Double-click the generated HTML to open in any browser.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gen_scanner.config import LEARNING_DIR, TIMEZONE

DASHBOARD_DIR = ROOT / "dashboard"
DASHBOARD_DIR.mkdir(exist_ok=True)
OUTPUT_HTML = DASHBOARD_DIR / "daily_dashboard.html"

SETUPS_LOG      = LEARNING_DIR / "setups_log.csv"
DAILY_STATS     = LEARNING_DIR / "daily_stats.csv"
OUTCOMES_LOG    = LEARNING_DIR / "outcomes_log.csv"
PERFORMANCE_LOG = LEARNING_DIR / "performance_log.csv"


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _sf(v, d: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "", "nan") else d
    except Exception:
        return d


def _compute_today_from_setups(today_setups: list[dict], today: str) -> dict:
    """Compute today KPI stats directly from setups_log.csv rows.
    Fallback used when daily_stats.csv has no entry yet (scanner still running,
    or stopped without a clean Ctrl+C that calls accumulator.finalize()).
    """
    from collections import defaultdict
    cycles = len(set(r.get("cycle", "") for r in today_setups))
    states: defaultdict = defaultdict(int)
    grades: defaultdict = defaultdict(int)
    dirs:   defaultdict = defaultdict(int)
    best_rr, best_rr_sym, best_rr_dir = 0.0, "", ""
    times = sorted(r.get("time", "") for r in today_setups if r.get("time"))
    best: dict[str, dict] = {}
    for r in today_setups:
        key = f"{r.get('symbol')}|{r.get('direction')}"
        if key not in best or _sf(r.get("score")) > _sf(best[key].get("score")):
            best[key] = r
    unique = list(best.values())
    for r in unique:
        states[r.get("state", "AVOID")] += 1
        grades[r.get("grade", "Skip")] += 1
        dirs[r.get("direction", "")] += 1
        rr = _sf(r.get("rr"))
        if rr > best_rr:
            best_rr, best_rr_sym, best_rr_dir = rr, r.get("symbol",""), r.get("direction","")
    return {
        "date":                   today,
        "total_cycles":           cycles,
        "total_symbols_evaluated": len(unique),
        "total_triggered":        states.get("TRIGGERED", 0),
        "total_ready":            states.get("READY_CONTINUATION", 0),
        "total_watchlist":        states.get("WATCHLIST", 0),
        "total_avoid":            states.get("AVOID", 0),
        "aplus_count":            grades.get("A+", 0),
        "a_count":                grades.get("A", 0),
        "b_count":                grades.get("B", 0),
        "skip_count":             grades.get("Skip", 0),
        "long_triggered":  sum(1 for r in unique if r.get("direction")=="long"  and r.get("state")=="TRIGGERED"),
        "short_triggered": sum(1 for r in unique if r.get("direction")=="short" and r.get("state")=="TRIGGERED"),
        "long_ready":      sum(1 for r in unique if r.get("direction")=="long"  and r.get("state")=="READY_CONTINUATION"),
        "short_ready":     sum(1 for r in unique if r.get("direction")=="short" and r.get("state")=="READY_CONTINUATION"),
        "best_rr_setup":   round(best_rr, 2) if best_rr else "",
        "best_rr_symbol":  best_rr_sym,
        "best_rr_direction": best_rr_dir,
        "session_start":   times[0][:5] if times else "",
        "session_end":     times[-1][:5] if times else "",
    }


def _build_data() -> dict:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    all_setups   = _load(SETUPS_LOG)
    daily_stats  = _load(DAILY_STATS)
    outcomes_all = _load(OUTCOMES_LOG)
    perf_all     = _load(PERFORMANCE_LOG)

    today_setups   = [r for r in all_setups   if r.get("date") == today]
    today_outcomes = [r for r in outcomes_all if r.get("date") == today]
    # Use daily_stats.csv if scanner stopped cleanly (Ctrl+C wrote it).
    # Otherwise compute live from setups_log.csv — covers post_market.bat run
    # while scanner is still running or after a forced close.
    today_stats = next((r for r in daily_stats if r.get("date") == today), None)
    if today_stats is None and today_setups:
        today_stats = _compute_today_from_setups(today_setups, today)
        print(f"  [DASHBOARD] Computing today stats from setups_log.csv ({len(today_setups)} rows)")
    elif today_stats is None:
        today_stats = {}

    # Deduplicate setups — best score per sym+dir
    best: dict[str, dict] = {}
    for r in today_setups:
        key = f"{r['symbol']}|{r['direction']}"
        if key not in best or _sf(r.get("score")) > _sf(best[key].get("score")):
            best[key] = r
    unique_setups = sorted(best.values(), key=lambda r: _sf(r.get("score")), reverse=True)

    # Outcomes today
    taken     = [r for r in today_outcomes if r.get("result") in ("WIN","LOSS","SCRATCH")]
    wins_t    = len([r for r in taken if r.get("result") == "WIN"])
    losses_t  = len([r for r in taken if r.get("result") == "LOSS"])
    net_r_t   = sum(_sf(r.get("actual_pnl_r")) for r in taken)
    wr_t      = round(wins_t / len(taken) * 100, 1) if taken else 0

    # All-time perf
    all_trades_total = sum(_sf(r.get("total_trades")) for r in perf_all)
    all_wins_total   = sum(_sf(r.get("wins"))          for r in perf_all)
    net_r_total      = sum(_sf(r.get("net_r"))         for r in perf_all)
    wr_total         = round(all_wins_total / all_trades_total * 100, 1) if all_trades_total > 0 else 0

    # Chart data: last 30 days net R
    chart_labels = [r.get("date","") for r in perf_all[-30:]]
    chart_net_r  = [_sf(r.get("net_r")) for r in perf_all[-30:]]
    chart_wr     = [_sf(r.get("win_rate_pct")) for r in perf_all[-30:]]

    # Daily stats chart (last 10 days) — merge daily_stats with today's live data
    all_daily = [r for r in daily_stats if r.get("date") != today]
    if today_stats and today_stats.get("total_cycles", 0):
        all_daily.append(today_stats)
    ds_labels    = [r.get("date","") for r in all_daily[-10:]]
    ds_triggered = [int(_sf(r.get("total_triggered"))) for r in all_daily[-10:]]
    ds_ready     = [int(_sf(r.get("total_ready")))     for r in all_daily[-10:]]

    return {
        "today":               today,
        "generated_at":        datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M ET"),
        "today_stats":         today_stats,
        "unique_setups":       unique_setups,
        "today_outcomes":      today_outcomes,
        "taken":               taken,
        "wins_today":          wins_t,
        "losses_today":        losses_t,
        "net_r_today":         round(net_r_t, 2),
        "win_rate_today":      wr_t,
        "all_trades":          int(all_trades_total),
        "all_wins":            int(all_wins_total),
        "net_r_total":         round(net_r_total, 2),
        "win_rate_total":      wr_total,
        "perf_history":        perf_all[-10:],
        "chart_labels":        chart_labels,
        "chart_net_r":         chart_net_r,
        "chart_wr":            chart_wr,
        "ds_labels":           ds_labels,
        "ds_triggered":        ds_triggered,
        "ds_ready":            ds_ready,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>15+5 Scanner Dashboard — {today}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --border: #30363d; --text: #e6edf3; --text2: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --blue: #58a6ff; --purple: #bc8cff; --orange: #ffa657;
    --radius: 10px;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }}
  .header {{ background: linear-gradient(135deg, #1a3a2a 0%, #0d1117 60%); padding: 24px 32px; border-bottom: 1px solid var(--border); }}
  .header h1 {{ font-size: 22px; font-weight: 700; color: var(--green); }}
  .header .sub {{ color: var(--text2); font-size: 13px; margin-top: 4px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; margin-left: 8px; }}
  .badge-long  {{ background: #0d3324; color: var(--green); border: 1px solid var(--green); }}
  .badge-short {{ background: #3d0f0f; color: var(--red);   border: 1px solid var(--red);   }}
  .badge-aplus {{ background: #1a3a2a; color: var(--green); }}
  .badge-a     {{ background: #1a2f1a; color: #80e080; }}
  .badge-b     {{ background: #2d2400; color: var(--yellow); }}
  .badge-skip  {{ background: #1a1a1a; color: var(--text2); }}
  .layout {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; padding: 24px 32px; }}
  .layout-2col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 32px 24px; }}
  .layout-3col {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; padding: 0 32px 24px; }}
  .layout-full {{ padding: 0 32px 24px; }}
  .card {{ background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }}
  .card h3 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text2); margin-bottom: 12px; }}
  .stat-big {{ font-size: 36px; font-weight: 700; line-height: 1; }}
  .stat-sub {{ font-size: 12px; color: var(--text2); margin-top: 6px; }}
  .green {{ color: var(--green); }}
  .red   {{ color: var(--red);   }}
  .yellow{{ color: var(--yellow);}}
  .blue  {{ color: var(--blue);  }}
  .section-title {{ font-size: 16px; font-weight: 600; padding: 0 32px 12px; color: var(--text); border-bottom: 1px solid var(--border); margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 12px; color: var(--text2); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--bg3); }}
  .state-triggered {{ color: var(--green); font-weight: 700; }}
  .state-ready     {{ color: #80e080; }}
  .state-watchlist {{ color: var(--yellow); }}
  .state-avoid     {{ color: var(--text2); }}
  .result-win   {{ color: var(--green); font-weight: 700; }}
  .result-loss  {{ color: var(--red);   font-weight: 700; }}
  .result-scratch {{ color: var(--yellow); }}
  .result-missed  {{ color: var(--text2); }}
  .chart-wrap {{ position: relative; height: 220px; }}
  .perf-row {{ display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--border); }}
  .perf-row:last-child {{ border-bottom: none; }}
  .perf-date {{ font-size: 12px; color: var(--text2); width: 90px; }}
  .perf-bar {{ flex: 1; background: var(--bg3); border-radius: 4px; height: 8px; overflow: hidden; }}
  .perf-fill {{ height: 100%; border-radius: 4px; }}
  .perf-val  {{ font-size: 13px; font-weight: 600; width: 60px; text-align: right; }}
  .empty {{ color: var(--text2); text-align: center; padding: 32px; font-size: 13px; }}
  .tip {{ background: var(--bg3); border: 1px solid var(--border); border-left: 3px solid var(--blue); border-radius: var(--radius); padding: 14px 18px; margin: 0 32px 24px; font-size: 13px; color: var(--text2); }}
  .tip strong {{ color: var(--text); }}
</style>
</head>
<body>

<div class="header">
  <h1>📊 15+5 Unified Scanner Dashboard</h1>
  <div class="sub">Generated: {generated_at} &nbsp;|&nbsp; Date: {today} &nbsp;|&nbsp;
    <span class="badge badge-long">🟢 LONG</span>
    <span class="badge badge-short">🔴 SHORT</span>
  </div>
</div>

<!-- KPI Row -->
<div class="layout">
  <div class="card">
    <h3>Scan Cycles Today</h3>
    <div class="stat-big blue">{total_cycles}</div>
    <div class="stat-sub">{total_symbols} symbol evaluations</div>
  </div>
  <div class="card">
    <h3>Triggered Today</h3>
    <div class="stat-big green">{total_triggered}</div>
    <div class="stat-sub">🟢 {long_triggered} Long &nbsp;|&nbsp; 🔴 {short_triggered} Short</div>
  </div>
  <div class="card">
    <h3>Ready Today</h3>
    <div class="stat-big yellow">{total_ready}</div>
    <div class="stat-sub">Awaiting final trigger candle</div>
  </div>
  <div class="card">
    <h3>Trades Taken</h3>
    <div class="stat-big {pnl_color}">{trades_taken}</div>
    <div class="stat-sub">Win rate: {win_rate_today}% &nbsp;|&nbsp; Net: {net_r_today_str}</div>
  </div>
</div>

<!-- Performance Row -->
<div class="layout">
  <div class="card">
    <h3>Today P&amp;L (R)</h3>
    <div class="stat-big {pnl_color}">{net_r_today_str}</div>
    <div class="stat-sub">{wins_today}W / {losses_today}L / {net_r_today}R</div>
  </div>
  <div class="card">
    <h3>Today Win Rate</h3>
    <div class="stat-big {wr_color}">{win_rate_today}%</div>
    <div class="stat-sub">{trades_taken} trades logged</div>
  </div>
  <div class="card">
    <h3>All-Time Win Rate</h3>
    <div class="stat-big blue">{win_rate_total}%</div>
    <div class="stat-sub">{all_trades} total trades</div>
  </div>
  <div class="card">
    <h3>All-Time Net R</h3>
    <div class="stat-big {total_r_color}">{net_r_total_str}</div>
    <div class="stat-sub">Cumulative across all sessions</div>
  </div>
</div>

<!-- Charts -->
<div class="layout-2col">
  <div class="card">
    <h3>Daily Net R (last 30 sessions)</h3>
    <div class="chart-wrap"><canvas id="netRChart"></canvas></div>
  </div>
  <div class="card">
    <h3>Triggered + Ready Setups per Day (last 10)</h3>
    <div class="chart-wrap"><canvas id="setupChart"></canvas></div>
  </div>
</div>

<!-- Today's setups table -->
<div class="layout-full">
  <div class="section-title">🎯 Today's Setups — Best Per Symbol</div>
  <div class="card">
    {setups_table}
  </div>
</div>

<!-- Outcomes table -->
<div class="layout-full">
  <div class="section-title">📋 Trade Outcomes — {today}</div>
  <div class="card">
    {outcomes_table}
  </div>
</div>

<!-- Performance history -->
<div class="layout-2col">
  <div class="card">
    <h3>Performance by Day (last 10)</h3>
    {perf_bars}
  </div>
  <div class="card">
    <h3>Win Rate Trend</h3>
    <div class="chart-wrap"><canvas id="wrChart"></canvas></div>
  </div>
</div>

<div class="tip">
  <strong>💡 How to update this dashboard:</strong>
  After each session → run <code>log_outcome.bat</code> to enter your trade results →
  then run <code>generate_dashboard.bat</code> to rebuild this page.
  Or run <code>post_market.bat</code> which does both automatically.
</div>

<script>
const chartDefaults = {{
  responsive: true, maintainAspectRatio: false,
  plugins: {{ legend: {{ labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }} }},
  scales: {{
    x: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e', font: {{ size: 10 }} }} }},
    y: {{ grid: {{ color: '#21262d' }}, ticks: {{ color: '#8b949e', font: {{ size: 10 }} }} }}
  }}
}};

// Net R chart
new Chart(document.getElementById('netRChart'), {{
  type: 'bar',
  data: {{
    labels: {chart_labels_json},
    datasets: [{{
      label: 'Net R',
      data: {chart_net_r_json},
      backgroundColor: {chart_net_r_json}.map(v => v >= 0 ? '#1a4a2a' : '#4a1a1a'),
      borderColor:     {chart_net_r_json}.map(v => v >= 0 ? '#3fb950' : '#f85149'),
      borderWidth: 1, borderRadius: 3,
    }}]
  }},
  options: {{ ...chartDefaults }}
}});

// Setup frequency chart
new Chart(document.getElementById('setupChart'), {{
  type: 'bar',
  data: {{
    labels: {ds_labels_json},
    datasets: [
      {{ label: 'Triggered', data: {ds_triggered_json}, backgroundColor: '#1a4a2a', borderColor: '#3fb950', borderWidth: 1, borderRadius: 2 }},
      {{ label: 'Ready',     data: {ds_ready_json},     backgroundColor: '#2d2400', borderColor: '#d29922', borderWidth: 1, borderRadius: 2 }},
    ]
  }},
  options: {{ ...chartDefaults, scales: {{ ...chartDefaults.scales, x: {{ ...chartDefaults.scales.x, stacked: false }} }} }}
}});

// Win rate chart
new Chart(document.getElementById('wrChart'), {{
  type: 'line',
  data: {{
    labels: {chart_labels_json},
    datasets: [{{
      label: 'Win Rate %',
      data: {chart_wr_json},
      borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)',
      borderWidth: 2, pointRadius: 3, tension: 0.3, fill: true,
    }}]
  }},
  options: {{ ...chartDefaults }}
}});
</script>
</body>
</html>"""


def _state_cls(s: str) -> str:
    return {"TRIGGERED": "state-triggered", "READY_CONTINUATION": "state-ready",
            "WATCHLIST": "state-watchlist"}.get(s, "state-avoid")


def _grade_badge(g: str) -> str:
    cls = {"A+": "badge-aplus", "A": "badge-a", "B": "badge-b"}.get(g, "badge-skip")
    return f'<span class="badge {cls}">{g}</span>'


def _result_cls(r: str) -> str:
    return {"WIN":"result-win","LOSS":"result-loss","SCRATCH":"result-scratch"}.get(r,"result-missed")


def _build_setups_table(setups: list[dict]) -> str:
    if not setups:
        return '<div class="empty">No setups evaluated today. Run the scanner first.</div>'
    rows = ""
    for r in setups[:20]:
        icon   = "🟢" if r.get("direction") == "long" else "🔴"
        scls   = _state_cls(r.get("state",""))
        grade  = _grade_badge(r.get("grade","?"))
        rr     = r.get("rr","—")
        rr_str = f"{float(rr):.2f}R" if rr and rr != "—" else "—"
        rows += f"""<tr>
          <td><strong>{icon} {r.get('symbol','?')}</strong></td>
          <td>{r.get('direction','?').upper()}</td>
          <td>{grade}</td>
          <td class="{scls}">{r.get('state','?')}</td>
          <td>{r.get('score','?')}</td>
          <td>{r.get('entry_type','—')}</td>
          <td>{r.get('entry','—')}</td>
          <td>{r.get('stop','—')}</td>
          <td>{r.get('t1','—')}</td>
          <td style="color:var(--blue)">{rr_str}</td>
          <td style="color:var(--text2);font-size:11px">{str(r.get('note',''))[:60]}</td>
        </tr>"""
    return f"""<table>
      <thead><tr>
        <th>Symbol</th><th>Dir</th><th>Grade</th><th>State</th>
        <th>Score</th><th>Type</th><th>Entry</th><th>Stop</th>
        <th>T1</th><th>RR</th><th>Note</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_outcomes_table(outcomes: list[dict]) -> str:
    if not outcomes:
        return '<div class="empty">No outcomes logged today.<br>Run <code>log_outcome.bat</code> after your session to record trades.</div>'
    rows = ""
    for r in outcomes:
        icon   = "🟢" if r.get("direction") == "long" else "🔴"
        rcls   = _result_cls(r.get("result",""))
        pnl    = r.get("actual_pnl_r","")
        pnl_str = f"{float(pnl):+.2f}R" if pnl else "—"
        pnl_color = "var(--green)" if pnl and float(pnl) > 0 else ("var(--red)" if pnl and float(pnl) < 0 else "var(--text2)")
        rows += f"""<tr>
          <td><strong>{icon} {r.get('symbol','?')}</strong></td>
          <td>{r.get('direction','?').upper()}</td>
          <td>{r.get('grade','?')}</td>
          <td>{r.get('entry','—')}</td>
          <td>{r.get('stop','—')}</td>
          <td>{r.get('t1','—')}</td>
          <td>{r.get('rr_planned','—')}</td>
          <td>{r.get('actual_exit','—')}</td>
          <td style="color:{pnl_color};font-weight:700">{pnl_str}</td>
          <td class="{rcls}">{r.get('result','?')}</td>
          <td style="color:var(--text2);font-size:11px">{str(r.get('notes',''))[:50]}</td>
        </tr>"""
    return f"""<table>
      <thead><tr>
        <th>Symbol</th><th>Dir</th><th>Grade</th>
        <th>Entry</th><th>Stop</th><th>T1</th><th>Plan RR</th>
        <th>Exit</th><th>P&amp;L</th><th>Result</th><th>Notes</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_perf_bars(perf: list[dict]) -> str:
    if not perf:
        return '<div class="empty">No performance data yet.</div>'
    max_net = max(abs(_sf(r.get("net_r"))) for r in perf) or 1
    html = ""
    for r in perf:
        net  = _sf(r.get("net_r"))
        wr   = _sf(r.get("win_rate_pct"))
        t    = int(_sf(r.get("total_trades")))
        color = "#3fb950" if net >= 0 else "#f85149"
        width = min(100, abs(net) / max_net * 100)
        sign  = "+" if net >= 0 else ""
        html += f"""<div class="perf-row">
          <div class="perf-date">{r.get('date','?')}</div>
          <div style="color:var(--text2);font-size:11px;width:50px">{t} trades</div>
          <div class="perf-bar"><div class="perf-fill" style="width:{width:.0f}%;background:{color}"></div></div>
          <div class="perf-val" style="color:{color}">{sign}{net:.2f}R</div>
          <div style="color:var(--text2);font-size:11px;width:40px">{wr:.0f}%</div>
        </div>"""
    return html


def generate() -> None:
    data = _build_data()
    today  = data["today"]
    stats  = data["today_stats"]

    # Derived values for template
    total_cycles   = int(_sf(stats.get("total_cycles")))
    total_symbols  = int(_sf(stats.get("total_symbols_evaluated")))
    total_trig     = int(_sf(stats.get("total_triggered")))
    total_ready    = int(_sf(stats.get("total_ready")))
    long_trig      = int(_sf(stats.get("long_triggered")))
    short_trig     = int(_sf(stats.get("short_triggered")))
    trades_taken   = len(data["taken"])
    wins_t         = data["wins_today"]
    losses_t       = data["losses_today"]
    net_r_t        = data["net_r_today"]
    wr_t           = data["win_rate_today"]

    pnl_color      = "green" if net_r_t > 0 else ("red" if net_r_t < 0 else "yellow")
    wr_color       = "green" if wr_t >= 55 else ("yellow" if wr_t >= 40 else "red")
    net_r_today_str = f"{net_r_t:+.2f}R" if trades_taken else "—"

    total_r_color  = "green" if data["net_r_total"] > 0 else "red"
    net_r_total_str = f"{data['net_r_total']:+.2f}R" if data["all_trades"] else "—"

    html = HTML_TEMPLATE.format(
        today           = today,
        generated_at    = data["generated_at"],
        total_cycles    = total_cycles or "—",
        total_symbols   = total_symbols or 0,
        total_triggered = total_trig,
        total_ready     = total_ready,
        long_triggered  = long_trig,
        short_triggered = short_trig,
        trades_taken    = trades_taken,
        wins_today      = wins_t,
        losses_today    = losses_t,
        win_rate_today  = wr_t,
        net_r_today     = net_r_t,
        net_r_today_str = net_r_today_str,
        pnl_color       = pnl_color,
        wr_color        = wr_color,
        all_trades      = data["all_trades"],
        win_rate_total  = data["win_rate_total"],
        net_r_total_str = net_r_total_str,
        total_r_color   = total_r_color,
        setups_table    = _build_setups_table(data["unique_setups"]),
        outcomes_table  = _build_outcomes_table(data["today_outcomes"]),
        perf_bars       = _build_perf_bars(data["perf_history"]),
        chart_labels_json   = json.dumps(data["chart_labels"]),
        chart_net_r_json    = json.dumps(data["chart_net_r"]),
        chart_wr_json       = json.dumps(data["chart_wr"]),
        ds_labels_json      = json.dumps(data["ds_labels"]),
        ds_triggered_json   = json.dumps(data["ds_triggered"]),
        ds_ready_json       = json.dumps(data["ds_ready"]),
    )

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  ✅ Dashboard generated: {OUTPUT_HTML}")
    print(f"     Open in any browser to view your daily report.\n")


if __name__ == "__main__":
    generate()
