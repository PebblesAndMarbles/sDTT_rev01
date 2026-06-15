# sDTT Knowledge Base

Last updated: 2026-06-15

## Purpose
This document captures current understanding of super DTT (sDTT) source CSVs and related outputs in this workspace, with emphasis on how files are produced, what they contain, and how downstream tooling consumes them.

## Scope of this note
- Focuses on CSV artifacts and data flow in the Python tooling under this repo.
- Covers 1278 and 1280 pipeline outputs, HCCD APC enrichment, and flagging/visualization consumers.
- Intended as a living knowledge base: append new findings from future chats and investigations.

## High-level data lineage
1. Pipeline orchestrators run per configured site/layer/CD scope and write core CSVs to `integrated_output/`.
2. For 1278 D1V and F32 HCCD, an APC join step enriches HCCD data with APC attributes and writes `_APC` CSVs (full + 60-day variants).
3. `sDTT_flagging_engine.py` reads the APC-enriched HCCD CSV, applies chamber/product centering logic, writes daily chamber-centric outputs, and calls the visualizer.
4. `sDTT_product_flagging_engine.py` runs a separate product/litho-centric flow (home + sister factory capable) and writes product-focused CSVs.
5. `sDTT_flag_visualizer.py` renders PNGs from engine flags and also supports stand-alone ad hoc analysis that writes an ad hoc summary CSV.

## Primary source CSV families

### A) Core integrated outputs (pipeline products)
Typical files in `integrated_output/`:
- `1278sDTT_HCCD_D1V.csv`
- `1278sDTT_HCCD_F32.csv`
- `1278sDTT_DCCD_D1V.csv`
- `1278sDTT_DCCD_F32.csv`
- `1278sDTT_FCCD_D1V.csv`
- `1278sDTT_FCCD_F32.csv`
- `1280sDTT_HCCD_D1V.csv`
- `1280sDTT_DCCD_D1V.csv`
- `1280sDTT_FCCD_D1V.csv`

What they represent:
- Joined and normalized per-wafer sDTT datasets with WEC, SPC, ALLSTATS, and measurement/statistics context.
- Produced by pipeline/data-processing scripts (not manually curated).

### B) APC-enriched HCCD output (1278 D1V + F32)
Primary files:
- `1278sDTT_HCCD_D1V_APC.csv`
- `1278sDTT_HCCD_F32_APC.csv`
- `1278sDTT_HCCD_D1V_60day_APC.csv`
- `1278sDTT_HCCD_F32_60day_APC.csv`

What it adds:
- APC columns such as `APC_B_TOOL`, `APC_SETTING_USED`, `APC_OPENRUNS`, `APC_FB_SUC`, `APC_AREA`, `APC_PRODGROUP`, plus additional APC attributes.
- This is the main input for chamber/product flagging logic.

Operational behavior note:
- The 60-day APC outputs are not frozen snapshots; they are refreshed by normal incremental APC join runs.

Related exploratory variants may appear:
- `1278sDTT_HCCD_D1V_APC_EXPLORATORY_...csv`

### C) Flagging and analysis outputs (derived)
Examples:
- `sDTT_flags_HCCD_D1V_YYYYMMDD.csv`
- `sDTT_chamber_flag_detail_YYYYMMDD.csv`
- `sDTT_product_flags_YYYYMMDD.csv`
- `sDTT_product_flag_detail_YYYYMMDD.csv`
- `sDTT_adhoc_flags_YYYYMMDD.csv`

These are downstream products, not raw source inputs.

Observed producer mapping:
- `sDTT_flagging_engine.py` writes `sDTT_flags_HCCD_D1V_YYYYMMDD.csv` and `sDTT_chamber_flag_detail_YYYYMMDD.csv`.
- `sDTT_product_flagging_engine.py` writes product outputs (for example `sDTT_product_flags_YYYYMMDD.csv`, `sDTT_product_flag_detail_YYYYMMDD.csv`).
- `sDTT_flag_visualizer.py` ad hoc mode writes `sDTT_adhoc_flags_YYYYMMDD.csv`.

### D) Intermediate query/debug artifacts
Located under `integrated_output/query_files/` (and historically under `debug/1278 QUERY FILES/`).
Examples include:
- `allstats_no_pivot_or_attr_split_*.csv`
- `allstats_pivot_chunk*_*.csv`
- `measurements_no_pivot_or_attr_split_*.csv`
- `lot_run_card_for_spcsid_*.csv`
- alias and operation mapping CSVs

These are staging/intermediate files used during pipeline build and troubleshooting.

## Naming conventions (observed)
- Core output pattern: `<tech>sDTT_<CDLEVEL>_<SITE>.csv`
- APC-enriched pattern: `<tech>sDTT_HCCD_<SITE>_APC.csv` and `<tech>sDTT_HCCD_<SITE>_60day_APC.csv`
- Date-stamped analytics: `sDTT_<artifact>_YYYYMMDD.csv`
- Chunk/intermediate files often include `chunk`, site suffix, or layer suffix.

## Key column groups in source CSVs

### Common core identifiers
- `DATA_COLLECTION_TIME`
- `WAFER_ID`, `SPC_LOT`, `LOT7`, `LOT_TYPE`
- `SUBENTITY`, `SUBENTITY_END_TIME`
- `WEC_OPERATION`, `WEC_RECIPE`, `WEC_LAYER`
- `PRODUCT`, `PRODUCT_GROUP`
- `PROD_MOP`, `PROD_MOP_PILOT`

### ALLSTATS fields (examples)
- `ALLSTATS_MEAN_DTT_VALUE`
- `ALLSTATS_MEAN_TARGET_VALUE`
- `ALLSTATS_SIGMA_DTT_VALUE`
- `ALLSTATS_DYNWAFER`
- `ALLSTATS_OPERATION`, `ALLSTATS_PILOT_NAME`

### STATISTICS fields (examples)
- `STATISTICS_MEAN_DTT_VALUE`
- `STATISTICS_MEAN_DTT_CENTERLINE`, `STATISTICS_MEAN_DTT_UCL`, `STATISTICS_MEAN_DTT_LCL`
- `STATISTICS_SIGMA_DTT_VALUE`
- related chart metadata and rule/validity columns

### APC fields (present in `_APC` file)
- `APC_B_TOOL`
- `APC_SETTING_USED`
- `APC_OPENRUNS`
- `APC_FB_SUC`
- `APC_AREA`
- `APC_PRODGROUP`
- additional APC attribute columns (e.g., `APC_B_TOOL_RS`, `APC_CALCULATED_SETTING`)

## Important filters and semantics used by current tooling

### In flagging/visualization workflows
- `ALLSTATS_DYNWAFER == DYNWAFER_001` is treated as the control-chart point population.
- Rows with null `STATISTICS_MEAN_DTT_VALUE` are dropped for centering analysis.
- `IS_BSL` is derived from `APC_PRODGROUP` prefix (`BSL...`).
- B-tool regime detection uses APC B-tool behavior in BSL rows and is separate from MAD outlier exclusion.

Important nuance (current code behavior):
- Main engine flow applies the `ALLSTATS_DYNWAFER == DYNWAFER_001` filter before visualization.
- Stand-alone ad hoc visualizer path currently does not explicitly apply the DYNWAFER filter; it drops null `STATISTICS_MEAN_DTT_VALUE` and then computes chamber-window logic.

### Ad hoc layer matching
- Ad hoc visualizer resolves layer by suffix matching on `WEC_LAYER` (`endswith(layer_short)`).
- Layer-short inputs must align with actual suffix style used in data (for example `MT9H` vs `M09`).

### Ad hoc visualizer controls and outputs (current)
- Input list is configured in `ADHOC_COMBINATIONS` as `(SUBENTITY, layer_short)` tuples.
- Optional recency limiter `ADHOC_LOOKBACK_DAYS` is available; `None` or `<=0` disables lookback.
- One PNG is written per resolved tuple in `flag_images/`, prefixed with `ADHOC_`.
- Combined ad hoc CSV is written to `integrated_output/sDTT_adhoc_flags_YYYYMMDD.csv`.
- Current ad hoc CSV columns are:
	- `SUBENTITY`
	- `Metal Layers`
	- `DELTA(nm) NEEDED`
	- `Current B_Tool`
	- `New BTOOL`

## APC join specifics worth preserving
- APC join is active for 1278 D1V and F32 HCCD flows.
- Join logic includes dedup/priority handling for ambiguous APC records.
- PM-safe SUBENTITY matching is enabled in production pipeline defaults (`use_subentity_pm_match=True`) to prevent PM-token switching.
- Special handling exists for UBE area rows where packed APC values must be split and chamber-indexed.

## Pipeline operational notes
- Orchestrators write final outputs to `integrated_output/` and intermediate artifacts to `integrated_output/query_files/`.
- Configurable lookback (`days`) exists at pipeline level.
- 1278 pipeline runs APC join for both D1V and F32 HCCD (full + 60-day variants) using manifest-based incremental mode.
- Production default enables PM-safe APC behavior (`use_subentity_pm_match=True`).
- 1280 pipeline is configured without APC join.

## Flagging engine defaults to remember
- `sDTT_flagging_engine.py` default input is `integrated_output/1278sDTT_HCCD_D1V_APC.csv`.
- `MIN_PERSIST_RUNS` is currently set to `1` (confirmed B-tool step persistence requirement).
- `PRODUCT_FLAGS_ENABLED` is currently `True` in the chamber flagging engine.
- Chamber detail CSV contains in-window rows for only the final ranked chamber groups.

## Product engine specifics worth preserving
- `sDTT_product_flagging_engine.py` can harmonize D1V (home) and F32 (sister) datasets.
- Product target regime detection there is discrete-value based (any target-value change creates a new boundary), unlike threshold-based target-change detection in `sDTT_flagging_engine.py`.
- Uses volume qualification (`LOOKBACK_DAYS`, `VOLUME_MIN_RUNS`) before product centering tests.

## Current known consumers
- Flagging engine input default: `integrated_output/1278sDTT_HCCD_D1V_APC.csv`.
- Visualizer uses engine outputs and can run stand-alone ad hoc analysis from the same APC-enriched source.
- JMP/JSL downstream scripts consume integrated outputs and derived summaries.

See also:
- `markdown/APC_Knowledge_Base.md` for detailed APC query/join rules, PM-safe alignment behavior, and incremental merge semantics.

## Risks and caveats
- Column drift risk: source schemas are wide and evolve; downstream scripts rely on specific names.
- Large file constraints: some files exceed editor sync thresholds; inspect headers via shell when needed.
- Layer token ambiguity: mixed conventions (for example `M10`, `M10H`, `MT9H`) require careful matching.
- Date parsing quality directly affects regime/window logic.

## Suggested extension template for future updates
When adding new findings, append entries in this format:
- Date:
- Area: (pipeline/apc join/flagging/visualizer/jmp)
- Source files reviewed:
- New confirmed behavior:
- Open questions:
- Action items:

## Quick glossary
- sDTT: super DTT dataset and associated analysis workflow.
- WEC: process context columns (operation, recipe, layer, chamber lineage).
- ALLSTATS: summarized measurement context per wafer/point.
- STATISTICS: SPC chart/statistical values used for centering logic.
- APC: process control attributes (including B-tool settings and feedback success).
