# sDTT APC Knowledge Base

Last updated: 2026-06-15  
Scope: 1278 D1V + F32 HCCD datasets (full + 60-day variants, incremental APC mode); 1280 D1V currently has no APC join.

---

## 1. Overview

The APC join enriches each sDTT wafer-operation row with run-job parameters pulled
from the fab APC system.  The output columns are prefixed `APC_` and joined back to
the main sDTT CSV on `(WAFER_ID, WEC_OPERATION)`.

As of 2026-06, PM-safe SUBENTITY matching is locked on in the 1278 production
pipeline config (`use_subentity_pm_match=True`) to prevent PM-token switching on
split-chamber rows.

---

## 2. Database Tables

| Table | Role |
|---|---|
| `F_LOT_FLOW` | Lot / operation history.  Join key: `LOTOPERKEY`. |
| `F_WAFERSLOTHIST` | Wafer slot history; provides `WAFER` ID and sorter-date bracket. |
| `P_APC_RUNJOB_HIST` | One row per APC run-job transaction.  Carries `APC_DATA_ID`, `APC_OBJECT_NAME`, `APC_OBJECT_TYPE`, `CHANGE_TYPE`, `APC_JOB_TXN_TIME`. |
| `P_APC_TXN_DATA` | EAV table: one row per `(APC_DATA_ID, ATTRIBUTE_NAME)`.  All parameter values live here as strings in `ATTRIBUTE_VALUE`. |

### Standard join chain

```sql
F_LOT_FLOW h
  INNER JOIN F_WAFERSLOTHIST w  ON w.EXPECTED_LOT = h.LOT
                                AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE
                                                   AND w.NEXT_SORTER_ACTION_DATE
                                AND w.HISTORY_DELETED_FLAG = 'N'
  INNER JOIN P_APC_RUNJOB_HIST j ON j.LOTOPERKEY = h.LOTOPERKEY
  INNER JOIN P_APC_TXN_DATA   d  ON d.APC_DATA_ID = j.APC_DATA_ID
```

Filters always applied:
- `h.EXEC_FLAG NOT IN ('X','R','N')` — exclude cancelled/re-run operations
- `j.APC_OBJECT_TYPE = 'LOT'` — LOT-level transactions only
- `j.APC_OBJECT_NAME LIKE '<system>%'` — system prefix match

---

## 3. APC Systems

Two APC system names are queried, in priority order:

| Priority | System prefix | Notes |
|---|---|---|
| 1 | `AEPCMC` | Primary; covers MFGAMECT_FLOW_TEMP and AMECT_ICCR2 areas. |
| 2 | `AEPC2` | Secondary fallback; same area cascade applied for unmatched wafers. |

If a wafer is matched by AEPCMC, it is removed from the remaining list and AEPC2 is
never queried for it.

---

## 4. APC Area Models

There are two distinct area hierarchies used across the codebase.  Each represents
a different model for how parameters are stored in the DB.

### 4.1 Production model — `1278sDTT_D1V_HCCD_APC_JOIN.py`

Full cascade per chunk, tried in order, first match (wafer has non-null AREA result) wins:

```
MFGAMECT_FLOW_TEMP  →  AMECT_ICCR2  →  8AMEUBE  →  8AMEUBE_GAS  →  (no area filter)
```

| Area | Meaning | Special handling |
|---|---|---|
| `MFGAMECT_FLOW_TEMP` | Flow/temperature model.  Higher-fidelity; values may be vector or matrix payloads. | `_extract_first_value` and `_RAW` column preservation applied. |
| `AMECT_ICCR2` | Run-recipe level.  One set of scalars per wafer.  Broadest coverage. | Standard scalar extraction. |
| `8AMEUBE` | Universal Body Etch — parent entity covers all 6 PMs.  Values packed into one row. | UBE extraction required (see §7). |
| `8AMEUBE_GAS` | Gas-specific UBE variant; used by BM0H layer (op 252845) via AEPC2.  Same packed format as `8AMEUBE`. | UBE extraction required (see §7).  Added as explicit tier 2026-05-21. |
| no filter | Last resort when all area-filtered tiers return zero rows. | May return mixed/ambiguous rows; use with caution. |

> **Cascade bug (fixed 2026-05-21):** Previously, `results.append(df_result)` fired
> unconditionally before `_matched_wafers_require_area_and_btool` was evaluated.
> For MT5H wafers, FLOW_TEMP returned AREA but no B_TOOL, so the wafer cascaded to
> AMECT_ICCR2 which added a second row.  After dedup-by-TXN_DATE, AREA could come
> from FLOW_TEMP while B_TOOL came from AMECT_ICCR2 — cross-tier contamination.
> Fix: matched wafers are computed first; only rows for matched wafers are appended
> at each tier.  Unmatched wafers cascade clean.

### 4.2 Exploratory model — `1278sDTT_D1V_HCCD_APC_JOIN_EXPLORATORY.py`

Target + fallback approach; does NOT fall through to no-filter:

```
MFGAMECT_FLOW_TEMP  →  AMECT_ICCR2
```

| Area | Meaning | Notes |
|---|---|---|
| `MFGAMECT_FLOW_TEMP` | Flow/temperature model.  Values include vector and matrix payloads (e.g. `B_TOOL`, `M_ETCHRATE`). | Primary target for HCCD parameter analysis. |
| `AMECT_ICCR2` | Fallback when FLOW_TEMP has no hit for that wafer. | Scalars only; no matrix payloads expected. |

> **Why two models?**  
> MFGAMECT_FLOW_TEMP is the higher-fidelity source for HCCD analysis but does not
> have coverage for all wafers/chambers.  AMECT_ICCR2 has broader coverage but
> stores parameters in scalar form only.  The exploratory script prioritises the
> richer model and falls back to the scalar one to maximise fill rate.

---

## 5. Attribute List

### Core attributes (always queried)

```
AREA, B_TOOL, B_TOOL_RS, CALCULATED_SETTING, FB_SUC, LAMBDA_DRIFT,
LAMBDA_POSTPM, LAMBDA_POSTPM_TOOL, LAMBDA_TOOL, LOTID, M_ETCHRATE,
MACHINE, OPENRUNS, OPENRUNS_PART, OPERATION, PROCESS_OPN, PRODGROUP,
PRODUCT, SETTING_USED, SUBENTITIES, SUBENTITY
```

### Extended attributes (confirmed available, added in exploratory work)

```
B_PART, B_PART_PRIOR, B_TOOL_PRIOR, LAMBDA_PART, METRO_HILIMIT,
METRO_LOLIMIT, MINWAFERS_4_FULLTUNE, MODE, NPI_LAMBDA_PART,
NPI_TUNE_LOTLIMIT, POSTPM_LAMBDA, POSTPM_LAMBDA_CLN_LIMIT,
POSTPM_LAMBDA_TOOL, POSTPM_TUNE_LOTLIMIT, REFERENCE_SETTING
```

Output columns are prefixed `APC_` after pivot (e.g. `APC_B_TOOL`).

> **OPERATION collision** — The attribute `OPERATION` conflicts with the join key
> `APC_OPERATION`.  The pivot helper (`safe_pivot_with_prefix_fixed`) renames it to
> `OPERATION_ATTR` before pivoting to prevent a duplicate column.

---

## 6. Value Payload Shapes (MFGAMECT_FLOW_TEMP area)

Parameters from FLOW_TEMP can be stored in three shapes depending on the attribute
and chamber configuration.

| Shape | Example raw value | Extraction rule |
|---|---|---|
| Scalar | `"1.023"` | Use as-is. |
| Vector | `"[1.023,0.998]"` | Take first element (index `a`). |
| Matrix | `"[1.023,0.850;0.991,0.773]"` | Take top-left element (row 0, col 0 = `a`). |

### Known shapes per attribute (observed in fleet HCCD sample, 2026-05)

| Column | Typical shape | Notes |
|---|---|---|
| `APC_B_TOOL` | vector | One value per sub-chamber or per recipe. |
| `APC_M_ETCHRATE` | matrix | Rows = sub-entities, cols = etch rates. |
| `APC_SETTING_USED` | scalar | Occasional null-like strings. |
| `APC_B_TOOL_PRIOR` | scalar / vector | Validated available; shape may vary. |
| `APC_LAMBDA_*` | scalar | All lambda columns scalar in observed data. |

### Parsing implementation (`_exploratory` script)

```python
def _extract_first_value(value):
    text = _strip_brackets(value)   # removes outer [ ] and null-like strings
    if text is None:
        return None
    first_row = text.split(';', 1)[0].strip()   # matrix: take first row
    if ',' in first_row:
        return first_row.split(',', 1)[0].strip() # vector: take first element
    return first_row                              # scalar: return as-is
```

`_strip_brackets` handles: empty string, `[NULL]`, `nan`, `None`, `NULL`.

### Raw column preservation

Before extraction, the original DB payload is copied to `_RAW` columns so the full
vector/matrix value is never lost:

| Extracted column | Raw backup column |
|---|---|
| `APC_B_TOOL` | `APC_B_TOOL_RAW` |
| `APC_M_ETCHRATE` | `APC_M_ETCHRATE_RAW` |
| `APC_SETTING_USED` | `APC_SETTING_USED_RAW` |

---

## 7. UBE Subentity Extraction (8AMEUBE area — production model)

When `APC_AREA` is `8AMEUBE` or `8AMEUBE_GAS`, the values in certain columns are
packed with one entry per PM chamber (up to 6), delimited by:

| Column | Delimiter |
|---|---|
| `APC_B_TOOL` | `;` (semicolon) |
| `APC_B_TOOL_RS` | `;` (semicolon) |
| `APC_OPENRUNS` | `,` (comma) |

The correct index is derived from the wafer row's `SUBENTITY` column:
`AME425_PM3` → PM index = 3 - 1 = **2** (0-based).

Implementation: `apply_ube_subentity_extraction()` in `1278sDTT_D1V_HCCD_APC_JOIN.py`.  
This function is reused (via `importlib`) by the exploratory script.

---

## 8. Dedup Priority Logic

Multiple APC job transactions can exist for the same `(WAFER_ID, OPERATION)`.  The
dedup sort order picks the best single transaction per attribute:

| Priority level | Field | Prefer |
|---|---|---|
| 1 (highest) | Null-like value | Non-null first |
| 2 | Subentity match | Exact PM match (0) > parent entity (1) > other (2) |
| 3 | APC system | AEPCMC (1) > AEPC2 (2) |
| 4 | Change type | `UPDATEPARAMETERS` (1) > other (2) |
| 5 | Area | Target area (1) > other (2) |
| 6 (lowest) | TXN_DATE | Most recent last (ascending → first row = oldest; sorts before keep='first') |

After sorting, `drop_duplicates(subset=['WAFER_ID','APC_OPERATION','ATTRIBUTE_NAME'], keep='first')` selects one value per attribute per wafer-operation.

---

## 9. Pivot / Wide Format

`safe_pivot_with_prefix_fixed()` (in `1278sDTT_D1V_HCCD_APC_JOIN.py`) converts the
long EAV result to wide format:

- `pivot_table(index=['WAFER_ID','APC_OPERATION'], columns='ATTRIBUTE_NAME', values='ATTRIBUTE_VALUE', aggfunc='first')`
- All non-key columns are prefixed with `APC_`.
- Duplicate column detection and suffix-based resolution included as a safety net.

---

## 10. Merge Back to Source Rows

After pivot, the wide APC DataFrame is left-joined to the filtered source rows:

```python
df_filtered.merge(
    df_apc_wide,
    left_on=['WAFER_ID', 'WEC_OPERATION'],
    right_on=['WAFER_ID', 'APC_OPERATION'],
    how='left',
)
```

`APC_MATCHED` (bool) is added: True if any `APC_*` column is non-null for that row.

---

## 11. Incremental Mode (production script only)

When `1278sDTT_PIPELINE.py` writes `current_run_wafers_{site}.csv` (written for
both D1V and F32), the production APC join operates incrementally:

1. Load the existing `_APC.csv`.
2. Drop rows for wafers in the manifest (those re-processed this run).
3. Query APC only for the manifest wafer set.
4. Merge fresh results back in; dedup by `(WEC_LAYER, SPC_LOT, WID)` keeping most recent.

This avoids re-querying the full history on every nightly run.  The nightly config
uses `days=5` (SPC/WEC lookback) with `apc_query_lookback_days=10` as a secondary
safety net when the manifest is unavailable.  Both the full `_APC.csv` and the
`_60day_APC.csv` variants are updated incrementally each run.

Important operational note (validated June 2026): `_60day_APC.csv` is not an
independent frozen snapshot. It is refreshed by normal daily pipeline runs through
the same incremental APC path as the full dataset variant.

### PM-safe lock-in status

- Current production default in `1278sDTT_PIPELINE.py` is `use_subentity_pm_match=True`.
- The CLI flag `--use-subentity-pm-match` still exists for explicit runs, but pipeline
  default already enables PM-safe behavior without requiring the flag.
- A 7-day pipeline refresh on 2026-06-10 verified `pm_disagreement=0` on both D1V and
  F32 60-day APC outputs after lock-in.

---

## 12. Chunk Size

Default: **80 wafers per SQL query**.  Larger values risk hitting DB IN-list limits
or memory pressure on the PyUber connection.  Smaller values increase query count.

---

## 13. File Map

| File | Role |
|---|---|
| `1278sDTT_D1V_HCCD_APC_JOIN.py` | Production APC join.  Exposes `main_cascading_area_priority_final()`, `safe_pivot_with_prefix_fixed()`, `apply_ube_subentity_extraction()`, `_reorder_columns_apc()`. |
| `1278sDTT_D1V_HCCD_APC_JOIN_EXPLORATORY.py` | Standalone exploratory script.  Uses FLOW_TEMP→ICCR2 strategy, extended attribute list, `_RAW` preservation, matrix extraction.  Imports helpers from production join via `importlib`. |
| `1278sDTT_PIPELINE.py` | Orchestrator; calls `main_cascading_area_priority_final()` with manifest path for incremental mode. |
| `1280sDTT_D1V.py` | 1280 node processing — **no APC join**. |

---

## 14. Known Gaps / Future Work

- Matrix extraction (`_extract_first_value`) always picks element `[0,0]`.  If the
  per-chamber index is needed (similar to UBE), a PM-index-aware extraction would
  be required.
- The pipeline summary log message still says "Area cascade: AMECT_ICCR2 → 8AMEUBE →
  no area filter" — this is a stale string literal in the logging code and does not
  reflect the actual five-tier cascade.  Low priority cosmetic fix.
- 1280 node has no APC join; if needed, the same pattern from 1278 applies but the
  correct DB area string(s) for 1280 chambers must be confirmed first.
- The `_60day_APC.csv` incremental mode retains existing rows for wafers not in the
  current manifest. Rows that have aged out of the 60-day SPC/WEC window are not
  actively evicted from the APC file. The file may drift slightly over time; this is
  typically harmless for JMP reporting, but periodic full 60-day rebuilds can be used
  when strict window hygiene is required.

---

## 15. MT5H Strict Match Mode

Operation `263067` (test name `8CD.FCCD.MT5H`, layer `MT5`) requires special
handling because FLOW_TEMP queries often return an AREA row but no B_TOOL for these
wafers.  Under the standard cascade this caused false "matched" results that blocked
correct AMECT_ICCR2 data from being used.

### Configuration

`PIPELINE_CONFIG['require_area_btool_for_match_ops'] = ['263067']`

This is wired through `build_config()` → `finalize_site_data()` → `run_apc_join(require_area_btool_for_match_ops=...)`.

### Behaviour

For operations listed in `require_area_btool_for_match_ops`, a wafer is only
considered **matched** at a given cascade tier when **both** `AREA` and `B_TOOL`
are non-null in the query result.  Rows for unmatched wafers are discarded at that
tier and the wafer cascades cleanly to the next tier.

Implementation: `_matched_wafers_require_area_and_btool()` in
`1278sDTT_D1V_HCCD_APC_JOIN.py`.  Logic change lives in
`try_apc_system_with_area_cascade()`.
