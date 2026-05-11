# sDTT APC_B_TOOL Regime Start Logic (Current Implementation)

## Scope
This note documents the current implemented logic used to determine when a new APC_B_TOOL regime has begun, and how that interacts with MAD outlier handling.

Primary code paths:
- Engine logic: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L211)
- Visualizer mirror for spike labeling: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L91)

## Current Regime Confirmation Rule
Configured constants:
- `B_TOOL_STEP_THRESHOLD = 0.3` at [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L21)
- `MIN_PERSIST_RUNS = 1` at [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L22)

For each chamber x layer (`SUBENTITY`, `WEC_LAYER`) on `APC_FB_SUC==1` rows with non-null `APC_B_TOOL`, sorted by time:

1. A candidate step at position `i` is detected when:
   - `abs(B[i] - B[i-1]) >= B_TOOL_STEP_THRESHOLD`
2. The candidate is confirmed only if there are at least `MIN_PERSIST_RUNS` following rows and all of them remain near the new level:
   - `j = i+1 ... i+MIN_PERSIST_RUNS`
   - `abs(B[j] - B[i]) < B_TOOL_STEP_THRESHOLD` for all required `j`

Implementation references:
- Candidate step check: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L250)
- Required following-run window: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L254)
- Stability test across following runs: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L259)

### Effective minimum row count
With `MIN_PERSIST_RUNS = 1`, confirmation needs:
- the step row itself (`i`), plus
- 1 following row (`i+1`)

So a new regime needs at least 2 rows total from the step point onward to be confirmed.

If only 1 post-step point exists, the candidate cannot be confirmed and remains unconfirmed.

## What the Visualizer Calls a Spike
The visualizer reproduces the same confirmation rule to mark unconfirmed steps as `SPIKE`:
- Spike detection helper: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L91)
- Same `end = i + 1 + MIN_PERSIST_RUNS` requirement: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L114)

A step-like move that does not meet persistence criteria is labeled `SPIKE`.

## Can MAD Outliers Count Toward Regime Confirmation?
Short answer: No.

Reason:
- B_TOOL regime detection is performed in Phase 3: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L606)
- MAD outlier filtering (`_keep_mask_after_mad`) is performed later in Phase 4 chamber centering: [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L618), [sDTT_flagging_engine.py](sDTT_flagging_engine.py#L315)

Therefore, outlier status in `STATISTICS_MEAN_DTT_VALUE` does not affect whether B_TOOL regime confirmation occurs.

The same order is used in ad hoc visualizer flow:
- Detect adjustments first: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L825)
- Apply MAD mask later: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L846)

## Why You Can See "First Point = SPIKE" and "Second Point = OUTLIER"
Label precedence in the visualizer is:
1. `PRE_ADJUST`
2. `SPIKE`
3. `OUTLIER`
4. clean in-window chamber label

Reference: [sDTT_flag_visualizer.py](sDTT_flag_visualizer.py#L554)

Interpretation:
- The first point after a jump can be `SPIKE` if the jump is not yet persistently confirmed.
- A later point can be `OUTLIER` via MAD on DTT.
- These are different tests on different signals and at different phases.

## PM6 on M12 (AME409_PM6 on 640_M12)
After updating to `MIN_PERSIST_RUNS = 1`, this chamber/layer now confirms a new regime.

Validation snapshot (run with `c:/users/tbatson/My Programs/SQLPathFinder3/Python3/python.exe`):
- Engine `detect_btool_adjustments` result for `AME409_PM6` + `640_M12`:
   - `LAST_BTOOL_ADJ_DATE = 2026-03-30 10:43:37`
   - `WINDOW_START_DATE = 2026-03-30 10:43:37`
   - Regime confirmation status: `True`
- Ad hoc visualizer call `generate_adhoc_visualization('AME409_PM6', 'M12')` succeeded and produced:
   - `{'SUBENTITY': 'AME409_PM6', 'Metal Layers': 'M12', 'DELTA(nm) NEEDED': 0.8538, 'Current B_Tool': 1.141435}`
   - image output `ADHOC_AME409_PM6_640_M12.png`

Interpretation for this case under current settings:
- Two points in the new level are sufficient to confirm regime start (step row + one following row).
- MAD outlier handling still does not participate in B_TOOL regime detection.
