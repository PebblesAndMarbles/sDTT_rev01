# sDTT APC Technical Summary

Last updated: 2026-06-15

## Problem Statement
The APC enrichment path had chamber-side disagreement during earlier investigation cycles. Current production behavior reflects PM-safe matching and strict-match controls, with PM disagreement now treated as a regression signal to monitor rather than an open baseline issue.

For current verification, the active scope is D1V `MFGAMECT_FLOW_TEMP` + `AMECT_ICCR2` and F32 `MFGAMECT_FLOW_TEMP` only. BM0 is excluded from these verification steps for now.

### Current Validation Snapshot (post lock-in)
- PM-safe matching is locked in pipeline defaults (`use_subentity_pm_match=True`).
- 2026-06-10 7-day pipeline refresh confirmed `pm_disagreement=0` on both D1V and F32 60-day APC outputs.
- Routine monitoring still tracks `area_present_btool_missing` and `no_area_match` for coverage/quality context.

Interpretation: PM disagreement is no longer the dominant operational mismatch class after lock-in. Remaining classes are primarily no-area and area-present/btool-missing, which are monitored separately from PM alignment regressions.

## Current APC Pull / Filter / Join Design

### 1) Site-aware APC data source
- APC join supports both sites and chooses DB connection by site.
- D1V and F32 are explicitly threaded through APC join entry points from pipeline and generation flows.

### 2) Query scope control (performance + operational safety)
- APC query lookback limiter is supported (`apc_query_lookback_days` / CLI override).
- This trims query subset only, while output still preserves full source rows after merge.

### 3) Incremental update mode (nightly behavior)
- Run manifest (`current_run_wafers_<site>.csv`) is written from current-run HCCD source rows.
- Incremental APC mode re-queries only manifest wafers and retains untouched existing APC rows.
- Existing rows for wafers being refreshed are dropped/replaced to avoid stale duplication.
- Both full APC and 60-day APC variants are refreshed through this same incremental path.

### 4) Null-fill targeted backfill mode
- Optional `fill_null_col` mode re-queries only wafers missing a specific APC field.
- Non-null existing rows are preserved for efficiency.

### 5) F32-specific model scoping
- Intended rule: enrich only rows where `MODEL == MFGAMECT_FLOW_TEMP`.
- If `MODEL` column is missing in input, rule cannot be enforced and run proceeds with warning.

### 6) FLOW_TEMP strict-match enforcement
- FLOW_TEMP tier now uses strict `AREA + B_TOOL` qualification before a wafer is accepted as matched.
- This is enabled by default in the APC join path and threaded through the pipeline config.
- The goal is to prevent cross-tier contamination where AREA comes from FLOW_TEMP but B_TOOL is inherited from a lower tier.

### 6b) PM-safe SUBENTITY alignment
- PM token alignment between source `SUBENTITY` and APC `SUBENTITY`/`SUBENTITIES` is enabled by default in pipeline APC runs.
- This prevents split-chamber PM branch switching during LOT-object APC expansion.

### 7) APC result shaping and merge
- APC records deduplicated by `(WAFER_ID, APC_OPERATION, ATTRIBUTE_NAME)` keeping latest by timestamp.
- Long APC attributes pivoted to wide `APC_*` columns.
- Left merge back to source on wafer + operation keys.
- Key type coercion added to avoid int/object merge failures.

## Current Monitoring Focus
Current operational checks focus on:

1. PM disagreement regressions (expected baseline after lock-in: zero on recent windows).
2. Coverage-oriented classes (`no_area_match`, `area_present_btool_missing`) by model bucket.
3. Incremental refresh integrity for both full and 60-day APC outputs.

## Design Choices Already Made
- Prioritized operational continuity and speed:
  - Keep full source output shape.
  - Restrict DB query volume via lookback and incremental manifests.
  - Preserve existing APC rows unless explicitly refreshed.
- Added transparency:
  - Site-specific DB logging and summary metrics.
  - Warnings (instead of hard failures) for missing optional scoping columns.
- Excluded BM0 from the current verification pass so the active scope stays small and the strict-match change can be validated before expanding scope.

## Next Technical Direction
Maintain current behavior and monitor for regressions:

1. Keep PM-safe matching and FLOW_TEMP strict-match controls enabled in production defaults.
2. Track class summaries by site to detect PM disagreement reappearance quickly.
3. Use periodic 7-day/60-day refreshes when needed to re-baseline recent windows after any non-PM-safe run.
4. Keep 60-day vs full-scope usage explicit per downstream consumer (display/monitoring vs long-history analysis).

### Notes
- Some legacy logs still print a stale cascade summary string (`AMECT_ICCR2 -> 8AMEUBE -> no area filter`), while execution uses the current full cascade including FLOW_TEMP and 8AMEUBE_GAS tiers.
- For canonical implementation detail, use `markdown/APC_Knowledge_Base.md` and `1278sDTT_D1V_HCCD_APC_JOIN.py`.
