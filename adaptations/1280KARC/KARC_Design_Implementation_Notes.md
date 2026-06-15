# 1280 KARC Design And Implementation Notes

## Scope And Intent
This document captures key design decisions made in the 1280 KARC adaptation and how SQL and pipeline logic were changed to implement those decisions.

Current operating posture:
- FCCD-only extraction for KARC adaptation.
- RAW measurements OFF by default.
- MT1-focused output (current `layerRange` default `[1, 1]`).
- WEC enriched with operation-level constraints and fallbacks.

---

## 1. FCCD-Only Pipeline

### Design Choice
Limit KARC adaptation to FCCD (avoid production-impacting broad changes).

### Implementation
- `cd_levels` and `cd_alias_levels` set to FCCD.
- Alias generation and CD partitioning restricted to FCCD.

### SQL Impact
- SPC allstats/statistics/measurements set-name patterns constrained to FCCD families (LIKE patterns on `MEASUREMENT_SET_NAME`).

---

## 2. RAW Measurements Disabled By Default

### Design Choice
Prevent measurement join fanout and duplicate multiplication; use allstats/statistics as primary signal.

### Implementation
- `include_raw_measurements=False`.
- Measurements query/join skipped when RAW OFF.
- Wafers derived from allstats when RAW OFF.
- `MEASUREMENTS_*` columns removed from new and existing output data when RAW OFF.

### SQL Impact
- Measurements SQL remains available but is bypassed in RAW OFF mode.
- Allstats/statistics become authoritative source for wafer-level pull in RAW OFF runs.

---

## 3. MT2 Retrieval Recovery (Generalized Patterning)

### Design Choice
Broaden matching so MT variants are not accidentally excluded by naming drift.

### Implementation
- Layer-aware TEST_NAME filters added to lot prefetch and lot-run-card:
  - Supports `MTx` and `Mx`, ST/non-ST variants, optional trailing `H`.
- Set-name matching made tolerant with LIKE patterns (instead of brittle exact equality).

### SQL Impact
- `spclot_prefetch_query(...)` and `lot_run_card_query(...)` now include multi-pattern layer filter blocks.
- allstats/statistics use FCCD-centric set-name LIKEs rather than strict single literal names.

---

## 4. Structure Handling: SQL Filter Optional, Preserve In Joins

### Design Choice
Do not over-filter in SQL when structure payload naming is inconsistent; preserve structure dimension through pivot and joins.

### Implementation
- `use_structure_sql_filter=False` default.
- Query builders accept `use_structure_filter` flag and inject/remove structure predicates.
- STRUCTURE is preserved in allstats/statistics pivot index and join keys.
- Duplicate-control keys updated to avoid collapsing distinct structures.

### SQL Impact
- Structure predicates are conditionally included:
  - When ON: tolerant `LIKE` checks against `ATTRIBUTES/PARAMETERS/CHART_ATTRIBUTES`.
  - When OFF: no structure predicate in SQL; structure derived downstream from attributes.

---

## 5. Duplicate-Control Strategy (Upstream, Not End-Stage Only)

### Design Choice
Avoid many-to-many join explosion by deduplicating on semantic keys before merges.

### Implementation
- Pre-merge `drop_duplicates` on defined join keys in join stages.
- Existing CSV merge logic removes overlapping keys from prior rows before appending new.
- Duplicate subset updated to include STRUCTURE where available.

### SQL/Pipeline Impact
- Reduced fanout and stabilized record cardinality.
- Prevents stale row reintroduction from historical output snapshots.

---

## 6. WEC Reinstitution Under Alias Gaps

### Design Choice
Keep WEC enrichment available even when alias catalog is incomplete/missing for expected alias strings.

### Implementation
1. Strict alias lookup:
- `process_op_aliases_query(...)` from `F_OPERATION_ALIAS` using normalized alias matching (`UPPER(TRIM(...))`).

2. Relaxed alias fallback:
- `process_op_aliases_query_relaxed(...)` uses broader alias predicates (`LIKE '%alias%'`).

3. Direct OPERATION override fallback:
- Config-driven map:
  - `wec_operation_overrides` (for missing aliases)
  - includes `E_V0_MAIN_ETCH -> 269250` and `E_V1_MAIN_ETCH -> 269250`.
- If alias lookups return zero rows, override operations are injected as WEC ops.

4. No-op fallback mode (lot+wafer):
- If alias-op resolution still empty, optional fallback queries WEC by lot+wafer to enrich rows.
- Filtered by allowed operations when overrides are configured.

5. Operation hard-constraint in optimized WEC SQL:
- Added both:
  - `h.OPERATION IN (...)`
  - `c.OPERATION IN (...)`
- Prevents chamber-history cross-operation leakage within shared runs.

### SQL Impact
- WEC query path now supports strict, relaxed, direct-op, and no-op fallback modes.
- Operation constraints explicitly applied in WEC SQL to keep `WEC_OPERATION` consistent.

---

## 7. MT1 Operational Guardrail

### Design Choice
For current MT1-focused phase, enforce canonical WEC operation value in output.

### Implementation
- Save-time normalization sets MT1 alias rows (`SPC_ALIAS='L_M1_FCCD'`) to `WEC_OPERATION='269250'`.
- Cast to string first to avoid dtype warning on assignment.

### Pipeline Impact
- Output column-level compliance even when mixed legacy dtype/history exists.

---

## 8. Active Alias-Scope Purge In Final Output

### Design Choice
When run scope narrows (e.g., MT1-only), old out-of-scope rows must not remain in output CSV.

### Implementation
- In `save_cd_level_data(...)`, derive allowed aliases from active `layerRange` and `cd_level`.
- Filter BOTH new run data and existing CSV to allowed aliases before dedup/merge.

### Pipeline Impact
- Prevents stale MT2 (or other out-of-scope) rows from polluting current-scope outputs.
- Makes full-file summaries reflect current run scope.

---

## 9. Runtime Stability Note

### Observation
In some sessions, after successful processing/logging, a Python.Runtime shutdown `AccessViolationException` was observed.

### Mitigation Implemented
- Success-path hard exit after `main()` completion in `__main__` block (`os._exit(0)` in success-only `finally`).

### Rationale
- Prevents false-negative exit codes after successful output generation.

---

## 10. Current Defaults Summary

- `layerRange: [1, 1]` (MT1 only)
- `cd_levels: ['FCCD']`
- `include_raw_measurements: False`
- `use_structure_sql_filter: False`
- `enable_wec_alias_relaxed_fallback: True`
- `wec_operation_overrides` includes MT1 and MT2 main-etch aliases mapped to `269250`
- `enable_wec_noop_fallback: True`

---

## 11. Verification Patterns Used During Iteration

Typical validation checks:
- Layer/chunk health in logs:
  - lots retrieved
  - lot-run-card SPCS IDs
  - allstats/statistics raw row counts
  - chunk final row counts
- Structure spread checks:
  - group by `STRUCTURE`
  - group by `SPC_ALIAS, STRUCTURE`
- WEC operation compliance checks:
  - group by `WEC_OPERATION`
  - group by `SPC_ALIAS, WEC_OPERATION`
- Scope cleanliness checks:
  - group by `SPC_ALIAS` in final CSV

---

## 12. Tradeoffs

- Broader pattern matching improved retrieval robustness but requires stronger downstream keying and filters.
- WEC no-op fallback restores enrichment under alias-catalog drift but can be expensive; operation constraints and alias-scope controls reduce risk.
- Save-time normalization/alias-scope purge guarantees contractual output semantics for current phase.

---

## 13. JMP JSL Reporting Framework Notes (KARC FCCD Structure Dispo)

This section captures JSL implementation details that were unique to the KARC disposition reporting flow and required targeted debugging.

### 13.1 Fixed Structure Scope (UI + Runtime)

Design intent:
- Restrict reporting to four approved structures only.

Implementation details:
- `allowed_structures = {"NESTVIA_PR", "NESTVIA_HM", "TRENCH", "ETE"}`.
- STRUCT tab source is hardwired to this set.
- Runtime enforcement removes non-approved rows before charting:
  - `dt << Select Where( !Contains( allowed_structures, Char( :STRUCTURE ) ) );`
  - `dt << Delete Rows;`

Why it matters:
- Prevents unsupported structures (for example, LCDU/legacy variants) from entering by-group charts and causing empty-pane behavior.

### 13.2 Structure-Keyed SPC Limit Maps

Design intent:
- Limits must bind to the by-group structure shown in each chart.

Implementation details:
- Six associative arrays keyed by structure name:
  - `structure_limits_mean_ucl`, `structure_limits_mean_cl`, `structure_limits_mean_lcl`
  - `structure_limits_sigma_ucl`, `structure_limits_sigma_cl`, `structure_limits_sigma_lcl`
- Limit loading runs inside `ProcessFLEETData` before chart formatting functions consume config.

Why it matters:
- Eliminates mismatch from prior layer-oriented keying in a structure-oriented report.

### 13.3 Correct Unique Group Discovery In JSL

Root cause resolved:
- `Associative Array( Column( bycol1 ) ) << Get Keys` returned no usable structure groups in this context.

Stable pattern used:
- `Summarize( dt, unique_groups = By( :STRUCTURE ) );`

Why it matters:
- `Summarize By` is the robust way here to obtain unique values for character by-groups.
- Once fixed, loop execution and `LIMIT MAP` debug traces resumed as expected.

### 13.4 Row Selection Expression For Group Loops

Resolved pattern:
- Use explicit column reference in row filter:
  - `rows = dt << Get Rows Where( :STRUCTURE == current_group );`

Why it matters:
- Prevents dynamic-column expression ambiguity during limit-map construction.

### 13.5 POR-First Limit Selection With Safe Fallback

Data behavior observed:
- `IS_POR` has `True/False`; non-POR rows commonly contain null limit columns.

Selection policy implemented:
- Pass 1: Prefer `IS_POR == True` rows with complete mean/sigma triples.
- Pass 2: If missing, fallback to any row with complete triples.

Why it matters:
- Preserves intended POR sourcing while still populating limits when POR rows are incomplete.

### 13.6 By-Group Label Normalization (Titles + Copy Panel)

Design intent:
- Keep user-visible labels stable and short in chart titles and copy/pic grouping.

Implementation details:
- `Extract_Layer_Value(...)` recognizes `STRUCTURE=` and `STRUCT=` prefixes first.
- `Format_Struct_Label(value)` outputs canonical `STRUCT=<UPPERCASE_VALUE>`.
- Trend title shape: `STATISTICS MEAN DTT VALUE 1280 KARC FCCD STRUCT=ETE`.
- Copy/pic grouping uses same canonical structure label.

Why it matters:
- Removes noisy by-group strings like `DATA_COLLECTION_TIME ...` from visible report headers.

### 13.7 Debug Strategy That Worked

Patterns used:
- Global debug gate: `debug_fleet_layout` and `Debug_Trace(...)`.
- Targeted traces around limit build:
  - group discovery count
  - per-group row counts (temporary)
  - final per-group map lines:
    - `LIMIT MAP | struct=... mean=[lcl,cl,ucl] sigma=[lcl,cl,ucl]`

Operational note:
- Temporary unconditional `Show(...)` lines were used for smoke checks, then removed after validation.

### 13.8 Final Outcome For Reporting Layer

Verified end state:
- 4-structure scope enforced.
- Limit arrays populated for each structure.
- `LIMIT MAP` traces present for ETE, NESTVIA_HM, NESTVIA_PR, TRENCH.
- Mean and sigma reference lines render on trend/variability charts.
- Titles and copy/pic labels consistently use `STRUCT=<name>` format.

---

## 14. KARC Operation Filtering And Legacy WEC Enrichment Removal

### 14.1 Operation Filtering Toggle

Design intent:
- Keep alias-driven behavior available for backward compatibility.
- Support deterministic subset runs using explicit operation lists.

Implementation details:
- New mode key in KARC config:
  - `operation_filtering_mode`: `alias-driven` (default) or `explicit-operations`
- New explicit operation payload:
  - `explicit_operations['spc']`
  - `explicit_operations['wec']`
- Explicit mode bypasses alias lookups and resolves operations directly from configured lists.

Current target subset:
- SPC operation: `270387`
- WEC operation: `269250`

### 14.2 Removed Legacy SED/ETCH WEC Paths

Design intent:
- De-scope legacy litho/etch enrichment not required for KARC structure disposition flow.

Removed behavior:
- SED operation alias splitting and SED WEC query path.
- HM_ETCH/MAIN_ETCH alias splitting and ETCH WEC query path.
- Minimal WEC post-processing function that derived scanner/reticle/etch columns.

Removed output columns:
- `SCANNER`
- `RETICLE`
- `AME_ETCH`
- `GTO_ETCH`

Why it matters:
- Reduces legacy coupling and simplifies WEC join flow to only required operation-scoped data.

### 14.3 Verification Pattern

Recommended rollout sequence:
1. Delete existing KARC output CSV for a clean validation pass.
2. Run short lookback (1–3 days) in explicit mode.
3. Confirm logs show resolved mode and operation lists.
4. Confirm output schema excludes removed legacy columns.
5. Proceed to longer lookback only after short-run validation passes.
