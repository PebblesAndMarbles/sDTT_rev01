# sDTT Product Flagging Engine

Last updated: 2026-06-15

## Purpose

`sDTT_product_flagging_engine.py` identifies **product-level DTT centering issues** across the sDTT dataset. Whereas the chamber flagging engine (`sDTT_flagging_engine.py`) groups wafers by process chamber and APC attributes, this engine groups by **`PROD_MOP_PILOT × WEC_LAYER`** — the product/process flow identity. It is designed to surface target bias that is product-specific rather than chamber-specific, and can harmonize data from two factories simultaneously (D1V as home, F32 as sister).

This engine does **not** consume or depend on APC columns. Its source CSVs are the HCCD integrated outputs, with or without APC enrichment (APC columns are simply ignored if present).

> **Pipeline note (June 2026):** F32 APC enrichment is in routine production and `1278sDTT_HCCD_F32_APC.csv` is available. This engine remains APC-agnostic, so using the base `1278sDTT_HCCD_F32.csv` as sister input is still valid and keeps the input contract simpler.

---

## Inputs

| Role | Default CSV path (hardcoded constant) |
|---|---|
| Home factory (D1V) | `integrated_output/1278sDTT_HCCD_D1V_APC.csv` |
| Sister factory (F32) | `integrated_output/1278sDTT_HCCD_F32.csv` |

Both paths can be overridden at call time via the `home_csv_path` and `sister_csv_path` arguments to `run_product_flagging_engine()`. Pass an empty string for `sister_csv_path` to run home-factory only.

> **Full CSV vs. 60-day CSV:** The `integrated_output/` folder also contains lighter-weight 60-day variants (e.g., `1278sDTT_HCCD_D1V_60day_APC.csv`). The product flagging engine should always use the **full CSVs**. Phase 2 (target regime detection) walks the complete chronological history of each product×layer group to locate where the current `ALLSTATS_MEAN_TARGET_VALUE` began. Truncating to 60 days risks cutting that walk mid-regime, which would corrupt `TARGET_CHANGE_DATE` and `N_PRE_CHANGE` for any product whose current target has been stable longer than 60 days. The 60-day files are appropriate for display-only consumers such as JMP/JSL dispo scripts.

### Key columns consumed

| Column | Usage |
|---|---|
| `DATA_COLLECTION_TIME` | Chronological ordering, regime detection, volume window, output date range |
| `STATISTICS_MEAN_DTT_VALUE` | Primary centering measurement for CI computation |
| `ALLSTATS_MEAN_TARGET_VALUE` | Defines target regime boundaries (any change = new regime) |
| `ALLSTATS_DYNWAFER` | Pre-filter: only `DYNWAFER_001` rows are kept |
| `PROD_MOP_PILOT` | Primary grouping dimension |
| `WEC_LAYER` | Primary grouping dimension |
| `ALLSTATS_CURRENT_RETICLE` | Lithography sub-group analysis (Phase 5) |
| `SCANNER` | Lithography sub-group analysis (Phase 5) |
| `FACTORY` | Injected by load step; tracks D1V vs F32 origin |

---

## Configuration Constants

```python
CI_CONFIDENCE   = 0.95   # t-distribution confidence interval for centering test
FLAGS_PER_LAYER = 5      # top-N flags kept per (FLAG_TYPE, WEC_LAYER) after ranking
MIN_FLAG_N      = 5      # minimum wafer count in clean sample to produce a flag

MAD_MULTIPLIER  = 3.0    # outlier gate: |value − median| > 3 × MAD → excluded

LOOKBACK_DAYS   = 7      # rolling window used to count recent runs for volume filter
VOLUME_MIN_RUNS = 15     # group must have STRICTLY MORE than 15 runs in the window
```

---

## Six-Phase Pipeline

### Phase 1 — Load and Harmonise (`load_and_harmonise`)

Reads each input CSV, stamps a `FACTORY` column (`D1V` or `F32`), concatenates into a single DataFrame, and applies standard preprocessing:

- Parse `DATA_COLLECTION_TIME` to datetime.
- Coerce `STATISTICS_MEAN_DTT_VALUE` and `ALLSTATS_MEAN_TARGET_VALUE` to numeric.
- **Filter** to `ALLSTATS_DYNWAFER == 'DYNWAFER_001'` (single chart-point wafers only).
- **Drop** rows with null `STATISTICS_MEAN_DTT_VALUE`.
- Ensure `PROD_MOP_PILOT` and `WEC_LAYER` are present; drop rows where either is null.

Rows where `ALLSTATS_DYNWAFER` is not present (column missing entirely) skip that filter with a warning rather than failing.

---

### Phase 2 — Discrete Target Regime Detection (`detect_target_regimes`)

For each `(PROD_MOP_PILOT, WEC_LAYER)` group, finds the **current target regime**: the unbroken trailing run of rows sharing the most recently observed `ALLSTATS_MEAN_TARGET_VALUE`.

Unlike the threshold-based approach in the chamber engine, **any change in target value** — regardless of magnitude — defines a new regime boundary. Only rows in the current (trailing) regime are eligible for flagging.

Adds three columns:

| Column | Meaning |
|---|---|
| `TARGET_REGIME_LATEST` | `True` for rows in the current (trailing) target regime |
| `TARGET_CHANGE_DATE` | Timestamp of the first row of the current regime (`NaT` when no prior regime exists) |
| `N_PRE_CHANGE` | Count of rows that preceded the current regime (`0` if the full history is a single regime) |

A special case: if the most recent target value is `NaN`, all rows in the group are treated as the current regime.

---

### Phase 3 — Volume Filter (`apply_volume_filter`)

Within `TARGET_REGIME_LATEST` rows, qualifies each `(PROD_MOP_PILOT, WEC_LAYER)` group for analysis.

A group is **VOLUME_QUALIFIED** if it has **strictly more than `VOLUME_MIN_RUNS` (15)** rows with `DATA_COLLECTION_TIME` in the past `LOOKBACK_DAYS` (7) days, measured backward from the group's most recent observation.

Groups below the threshold are silently excluded from Phases 4 and 5. This volume gate replaces the BSL/NPI distinction used in the chamber engine — product groups with thin recent history are not flagged.

---

### Phase 4 — Product Centering Flags (`build_product_flags`)

For each volume-qualified group:

1. **MAD outlier suppression** — wafers where `|DTT − median| > 3 × MAD` are excluded from the centering calculation but retained in the detail output tagged as `MAD_EXCLUDED = True`.
2. **95% CI (t-distribution)** — computed on the cleaned sample via `_centering_test` from `sDTT_utils`.
3. **Flag gate** — a flag is only raised if:
   - clean sample has ≥ `MIN_FLAG_N` (5) wafers, **and**
   - the CI entirely excludes zero (i.e., bias is statistically significant).

When both D1V and F32 wafers are present in the same group, `FACTORY` is labeled `COMBINED` and the factory mix is noted in `NOTES`.

Each flagged group produces one row in the product flags table with `FLAG_TYPE = 'PRODUCT_TARGET'`.

---

### Phase 5 — Reticle and Scanner Sub-Group Flags (`build_subgroup_flags`)

Within each volume-qualified group, the same centering test is run independently for each **lithography sub-group**:

| Source column | FLAG_TYPE | Identity column populated |
|---|---|---|
| `ALLSTATS_CURRENT_RETICLE` | `PRODUCT_RETICLE` | `RETICLE_ID` |
| `SCANNER` | `PRODUCT_SCANNER` | `SCANNER_ID` |

Sub-groups with blank/NaN labels, fewer than `MIN_FLAG_N` wafers after MAD cleaning, or CI straddling zero are silently skipped. This phase surfaces cases where a specific reticle or scanner is driving a centering offset within a product/layer combination.

---

### Phase 6 — Assemble and Rank (`assemble_and_rank`)

Combines the product flag table (Phase 4) and sub-group flag table (Phase 5) into a single output. Within each `(FLAG_TYPE, WEC_LAYER)` group:

- Flags are **sorted by `|MEAN_DTT_BIAS|` descending**.
- The **top `FLAGS_PER_LAYER` (5)** entries are kept.
- `PRIO` is assigned `1` through `N` (1 = largest absolute bias).

Final output is sorted by `FLAG_TYPE → WEC_LAYER → PRIO`.

---

## Outputs

Both files are written to the same directory as the home factory CSV (`integrated_output/`).

| File | Content |
|---|---|
| `sDTT_product_flags_YYYYMMDD.csv` | Ranked product flag summary, one row per flagged group |
| `sDTT_product_flag_detail_YYYYMMDD.csv` | Per-wafer detail for flagged groups (includes `MAD_EXCLUDED` and `FLAG_PASSED` columns) |

Detail rows are filtered to only include `(PROD_MOP_PILOT, WEC_LAYER)` pairs that survived Phase 6 ranking (i.e., no orphaned detail rows for flags that were ranked out).

### Output column reference

| Column | Description |
|---|---|
| `PRIO` | 1 = highest bias in this FLAG_TYPE × WEC_LAYER group |
| `FLAG_TYPE` | `PRODUCT_TARGET`, `PRODUCT_RETICLE`, or `PRODUCT_SCANNER` |
| `LAYER_SHORT` | Shortened layer token (derived from `WEC_LAYER` by splitting on `_`) |
| `WEC_LAYER` | Full WEC layer identifier |
| `PROD_MOP_PILOT` | Product MOP + pilot identifier |
| `N_WAFERS` | Count of wafers in the clean (post-MAD) sample |
| `MEAN_DTT_BIAS` | Mean DTT bias (nm) of the clean sample |
| `DELTA(nm) NEEDED` | Same as `MEAN_DTT_BIAS` — signed correction magnitude |
| `CI_LOWER` / `CI_UPPER` | 95% confidence interval bounds on the mean |
| `WINDOW_START_DATE` / `WINDOW_END_DATE` | Earliest and latest `DATA_COLLECTION_TIME` in the group |
| `N_OUTLIERS_EXCLUDED` | Wafers dropped by MAD gate |
| `TARGET_CHANGE_DATE` | Date the current target regime started (`NaT` if no prior regime) |
| `N_PRE_CHANGE` | Rows before the current regime (context for regime duration) |
| `FACTORY` | `D1V`, `F32`, or `COMBINED` |
| `RETICLE_ID` | Populated for `PRODUCT_RETICLE` flags; blank otherwise |
| `SCANNER_ID` | Populated for `PRODUCT_SCANNER` flags; blank otherwise |
| `Metal Layers` | Layer label derived from source data `LAYER` column (best-mode); blank for sub-group flags |
| `NOTES` | Free-text notes (outlier count, combined-factory detail) |

---

## Relationship to Other Engine Components

```
1278sDTT_HCCD_D1V_APC.csv  ──┐
                               ├──► sDTT_product_flagging_engine.py ──► sDTT_product_flags_YYYYMMDD.csv
1278sDTT_HCCD_F32.csv      ──┘                                     ──► sDTT_product_flag_detail_YYYYMMDD.csv
(or _F32_APC.csv)
```

- The **chamber flagging engine** (`sDTT_flagging_engine.py`) operates on `SUBENTITY` (chamber) groups and uses APC B-tool attributes. It runs independently.
- The product engine shares the statistical utility functions (`_mad`, `_centering_test`, `_is_flagged`, `_keep_mask_after_mad`) from `sDTT_utils.py` with the chamber engine.
- The **visualizer** (`sDTT_flag_visualizer.py`) does not currently consume product engine outputs directly; it renders chamber-centric views.

---

## Known Gaps and Pending Items

- **Sister factory file selection:** F32 now also has an APC-enriched CSV (`1278sDTT_HCCD_F32_APC.csv`), but since this engine is entirely APC-agnostic (APC attributes are chamber-level knobs, not product-level), there is no reason to prefer the APC file over the base `1278sDTT_HCCD_F32.csv`. The current `SISTER_FACTORY_CSV` constant pointing to the base file is correct. Either file is functionally equivalent as input; the base file is the simpler choice.
- **`LAYER` column availability:** `LAYER_SHORT` derivation relies on `_` splitting of `WEC_LAYER`; `Metal Layers` uses a mode of the `LAYER` column if present. If `LAYER` is absent from the source schema, `Metal Layers` will be blank in all outputs.
- **Reticle/scanner column availability:** If `ALLSTATS_CURRENT_RETICLE` or `SCANNER` are absent from source data, Phase 5 silently skips those dimensions (no error raised).
- **Detail CSV filter performance:** The per-row `apply` lambda in the detail-filtering step may be slow for very large detail DataFrames. A merge-based approach would be more performant at scale.
