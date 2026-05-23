# CHANGELOG — Unified 15+5 Scanner: v6 → v7
## Date: May 2026

### QC-1 — scanner.py: prev_close injected into df5 (CRITICAL fix)
- `bundle.prev_close` is now written to `df5["prev_close"]` after `add_indicators()`.
- Fixes gap_quality scoring component 13 which was always defaulting to 3/8.
- Impact: accurate gap quality scoring for both longs (gap-up) and shorts (gap-down).

### QC-2/3 — risk_targets.py: Stop hierarchy uses ranked priority fallback (CRITICAL fix)
- SHORT: iterates hierarchy in order (failed reclaim high → ORL band → EMA cluster →
  compression ceiling → wick cluster → VWAP emergency). Uses FIRST valid structural anchor.
- LONG: iterates (failed breakdown low → ORH support → EMA cluster → compression floor →
  wick cluster → VWAP bounce). Uses FIRST valid structural anchor.
- Previous behavior: `max()` / `min()` of ALL valid anchors caused VWAP (often far from
  entry) to dominate stop placement, systematically killing RR on valid setups.
- Impact: stops are now structurally correct and spec-compliant. Valid setups no longer
  blocked by artificially over-widened stops.

### QC-4 — utils.py: compute_pivots() extended with PDH, PDL, SMA200
- Added optional params: `pdh`, `pdl`, `sma200`.
- When provided, these appear as "PDH", "PDL", "SMA200" in the pivot dict.
- Impact: T1/T2 calculations now include prior day high/low and 200 SMA as key walls.
  Wall detection, RR gate, and Gate 5 proximity checks are more accurate.

### QC-5 — indicators.py: SMA200 computed in add_indicators()
- `df5["sma200"]` added using `rolling(200, min_periods=200).mean()`.
- Returns NaN until 200 bars available; the scanner only includes it as a pivot when valid.
- Impact: enables QC-4 SMA200 wall inclusion in live scans.

### QC-6 — scanner.py: SMA200, PDH, PDL passed to compute_pivots()
- Extracts `sma200_val` from last df5 row; guards against NaN before passing.
- Passes `pdh=bundle.prev_high`, `pdl=bundle.prev_low`, `sma200=sma200_arg`.

### QC-7 — scanner.py: pullback_held_ema enforced as TRIGGERED downgrade gate (HIGH fix)
- After preliminary state assignment, LONG TRIGGERED setups with `pullback_held_ema=False`
  are downgraded to READY_CONTINUATION.
- Spec (Step 4A): "Fail — Do Not Enter: Retest candle closed below EMA 20."
- Impact: scanner no longer signals TRIGGERED on longs where retest broke through EMA20.

### QC-8 — state_machine.py: _time_gate() is direction-aware (MEDIUM fix)
- Added `direction` parameter (default="both" for backward compatibility).
- LONG decision-window check: confirms HH or HL structure (higher highs or higher lows).
- SHORT decision-window check: confirms LL or LH structure (lower lows or lower highs).
- Previous behavior: checked lower lows/highs for BOTH directions — wrong for longs.

### QC-9 — state_machine.py: evaluate_final_eligibility() passes direction to _time_gate()
- `_time_gate(df5, prelim_state, direction)` — direction now flows through.

### QC-10 — learning_logger.py: pullback_held_ema added to SETUP_COLS
- Historical setup log now captures pullback depth quality for post-market analysis.
- Enables win-rate analysis by pullback_held_ema status over time.
