# NCD Design Notes (1278 D1V MT6)

## Purpose
This document captures the design decisions implemented for the standalone NCD pipeline:
- script: `1278sDTT_NCD_D1V.py`
- orchestrator: `1278sDTT_NCD_PIPELINE.py`
- output: `integrated_output/1278sDTT_NCD_MT6_D1V.csv`

The goal is to analyze AME chamber context at NCD measurement steps (WEC join), using validated NCD-specific monitor/measurement-set constraints.

## Scope
- Site: D1V
- Tech: 1278
- Layer: MT6 (single-layer implementation for now)
- NCD alias: `A_8M6_FC_NCD`

## Core Fixed Filters
These were intentionally hard-fixed to eliminate cross-set fanout and ambiguous joins:

- `FIXED_NCD_MEASUREMENT_SET_NAME = GTOBE.PARAMETERS_DTT.78.DER`
- `FIXED_NCD_MONITOR_SET_NAME = GTOBE.INLINE_CD.78.MON`

Applied in query paths:
- SPC lot prefetch and lot-run-card constrained by `MONITOR_SET_NAME`
- allstats/statistics (and previously raw measurements) constrained by `MEASUREMENT_SET_NAME`

## Observed NCD Probe Facts
From probe/log validation on NCD sessions:

- TEST_NAME seen: `8GTOBE.CD.MT6.B`
- Discovered measurement sets (probe):
  - `GTOBE.PARAMETERS_MSR.78.DER`
  - `GTOBE.PARAMETERS_DTT.78.DER`
  - `GTOBE.PARAMETERS.78`
  - `GTOBE.MSE.78`
- For this flow, only `GTOBE.PARAMETERS_DTT.78.DER` is used for query execution.

## Attribute/Pivot Strategy
NCD GTOBE data pivots by `STATISTICS` (for example `MEAN_DTT`, `SIGMA_DTT`) rather than legacy `CD_TERMS` assumptions.

Implemented behavior:
- allstats pivot key detection prioritizes `STATISTICS`
- statistics pivot key detection prioritizes `STATISTICS`
- guards remain for alternate keys (`CD_TERMS`, `PARAMETER_NAME`) for resilience

## WEC Alias Strategy
WEC context is joined with:
- HM etch alias: `E_8M6_HM_ETCH`
- SED alias: `L_8M6_SED`
- MAIN etch alias: derived pattern from HM alias

Pattern encoded:
- `E_8M{N}_HM_ETCH -> E_8V{N-1}_MAIN_ETCH`
- example: `E_8M6_HM_ETCH -> E_8V5_MAIN_ETCH`

This is implemented via `derive_main_etch_alias_from_hm_etch(...)` with a guard pin for expected MT6 output.

## Join Design
To prevent many-to-many blowups across SPC datasets:

- join key includes `MEASUREMENT_SET_NAME` in addition to:
  - `SPCS_ID`
  - `TEST_NAME`
  - `WAFER_ID`

This key is used when combining allstats and statistics.

## Raw Measurements Query Status
Current decision: disabled intentionally.

- The raw measurements query path (which generated `MEASUREMENTS_VALUE_*`) is disabled.
- Join path is now allstats + statistics only.
- This reduces extra fanout surfaces and complexity.

Downstream implication:
- no new `WAFER_RADIUS_*` columns from raw measurements
- no new ESC zone columns derived from `WAFER_RADIUS_*` for NCD-only runs
- legacy columns can still appear if carried forward from an existing merged CSV schema

## Runtime/Logging Notes
Windows console/file logging previously threw `UnicodeEncodeError` due to Unicode separators/arrows.

Implemented mitigations:
- UTF-8 log file handlers
- safe console handlers with replacement fallback for non-encodable chars

## Validation Snapshots
Recent validated outcomes include:
- clean reruns after deleting output CSV
- successful 1-day lookback runs (for example 103-row output)
- fixed filters confirmed in logs:
  - `MEASUREMENT_SET_NAME fixed: GTOBE.PARAMETERS_DTT.78.DER`
  - `MONITOR_SET_NAME fixed: GTOBE.INLINE_CD.78.MON`

## Expansion Guidance (Future Layers)
For extending MT6 to more layers (for example M6 to M14):

1. Generate per-layer NCD/HM/SED aliases from layer number.
2. Derive MAIN alias using the encoded shift rule (`M(N) -> V(N-1)`).
3. Keep fixed monitor/measurement-set constraints unless data-validation indicates a layer-specific exception.
4. Keep join key including `MEASUREMENT_SET_NAME`.
5. Decide explicitly whether raw measurements remain disabled for multi-layer mode.

## Non-Goals in Current Implementation
- APC join integration
- checkpoint/resume flow
- multi-layer loop in this script (still single-layer MT6)
- generalized monitor-set auto-discovery (monitor set is fixed by design)
