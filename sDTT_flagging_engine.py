import numpy as np
import pandas as pd
import logging
from datetime import datetime
from pathlib import Path
from scipy import stats
from sDTT_utils import _mad, _centering_test, _is_flagged, _keep_mask_after_mad


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Configuration constants ────────────────────────────────────────────────────

INPUT_CSV = (
    r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson"
    r"\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_D1V_APC.csv"
)

# APC_B_TOOL step / persistence detection
B_TOOL_STEP_THRESHOLD = 0.3   # minimum |delta| between consecutive BSL rows to be a candidate step
MIN_PERSIST_RUNS      = 1     # subsequent BSL runs at the new level required to confirm an adjustment

# Product target change detection
TARGET_CHANGE_THRESHOLD_NM = 0.1   # minimum |delta| in ALLSTATS_MEAN_TARGET_VALUE to mark a regime change

# Centering flag thresholds
CI_CONFIDENCE    = 0.95
FLAGS_PER_LAYER  = 5   # top-N flags kept per (FLAG_TYPE, WEC_LAYER); ranked PRIO 1-FLAGS_PER_LAYER
MIN_FLAG_N       = 5   # minimum wafers in assessment window to produce a flag

# Outlier suppression
MAD_MULTIPLIER = 3.0   # |value − median| > MAD_MULTIPLIER × MAD → treated as outlier

# Chamber confound detection
CONFOUND_DOMINANCE_THRESHOLD = 0.80   # single SUBENTITY must account for > this fraction

# Product flag gate — set False after sDTT_product_flagging_engine.py is validated
PRODUCT_FLAGS_ENABLED = True

# ── Output column order ────────────────────────────────────────────────────────

_OUTPUT_COLUMNS = [
    'PRIO', 'FLAG_TYPE', 'LAYER_SHORT', 'WEC_LAYER',
    'SUBENTITY', 'Metal Layers', 'DELTA(nm) NEEDED', 'Current B_Tool',
    'PROD_MOP_PILOT', 'APC_PRODGROUP',
    'N_WAFERS', 'MEAN_DTT_BIAS', 'CI_LOWER', 'CI_UPPER',
    'LAST_BTOOL_ADJ_DATE', 'WINDOW_START_DATE', 'WINDOW_END_DATE',
    'N_OUTLIERS_EXCLUDED', 'TARGET_CHANGE_DATE', 'N_PRE_CHANGE', 'NOTES',
]

# ── Statistical helpers — defined in sDTT_utils.py ───────────────────────────
# _mad, _centering_test, _is_flagged, _keep_mask_after_mad  (imported above)


# ── Phase 0: B_TOOL Persistence Calibration ───────────────────────────────────

def run_btool_persistence_calibration(df: pd.DataFrame) -> None:
    """
    Empirically validate MIN_PERSIST_RUNS against the actual B_TOOL step behaviour
    in the dataset.  Logs a calibration report and emits a WARNING if the configured
    constant appears poorly suited to the data.  Does not halt execution or modify df.
    """
    logger.info("=" * 70)
    logger.info("PHASE 0 — B_TOOL PERSISTENCE CALIBRATION")
    logger.info("=" * 70)

    if 'APC_B_TOOL' not in df.columns:
        logger.warning("  APC_B_TOOL column absent — calibration skipped")
        return

    bsl_df = (
        df[df['APC_FB_SUC'].eq(1) & df['APC_B_TOOL'].notna()]
        .sort_values(['SUBENTITY', 'WEC_LAYER', 'DATA_COLLECTION_TIME'])
    )

    if bsl_df.empty:
        logger.warning("  No APC_FB_SUC=1 rows with non-null APC_B_TOOL — calibration skipped")
        return

    group_sizes = bsl_df.groupby(['SUBENTITY', 'WEC_LAYER']).size()
    n_too_small = int((group_sizes < MIN_PERSIST_RUNS).sum())

    persistence_counts: list[int] = []

    for (_sub, _layer), grp in bsl_df.groupby(['SUBENTITY', 'WEC_LAYER'], sort=False):
        btool = grp['APC_B_TOOL'].to_numpy()
        n = len(btool)
        if n < 2:
            continue

        # Indices of candidate step events (row i where |diff from i-1| >= threshold)
        step_positions = [
            i for i in range(1, n)
            if abs(btool[i] - btool[i - 1]) >= B_TOOL_STEP_THRESHOLD
        ]
        if not step_positions:
            continue

        # For each step, count how many rows follow before the next step event
        sentinels = step_positions + [n]
        for k, pos in enumerate(step_positions):
            runs_at_new_level = sentinels[k + 1] - pos - 1
            persistence_counts.append(runs_at_new_level)

    if not persistence_counts:
        logger.info("  No candidate B_TOOL step events found — calibration N/A")
        logger.info("=" * 70)
        return

    arr = np.array(persistence_counts)
    total = len(arr)
    p = np.percentile(arr, [10, 25, 50, 75, 90])

    def pct_ge(k: int) -> float:
        return 100.0 * (arr >= k).sum() / total

    pct_at_threshold = pct_ge(MIN_PERSIST_RUNS)

    logger.info(f"  Step threshold:    {B_TOOL_STEP_THRESHOLD}")
    logger.info(f"  MIN_PERSIST_RUNS:  {MIN_PERSIST_RUNS}  (currently configured)")
    logger.info(f"  Candidate steps:   {total}")
    logger.info(f"  Persistence distribution (BSL runs at new level before next step):")
    logger.info(f"    p10={p[0]:.0f}  p25={p[1]:.0f}  p50={p[2]:.0f}  p75={p[3]:.0f}  p90={p[4]:.0f}")
    logger.info(f"  % persisting ≥  1 run:   {pct_ge(1):.1f}%")
    logger.info(
        f"  % persisting ≥ {MIN_PERSIST_RUNS:>2} runs:  "
        f"{pct_ge(MIN_PERSIST_RUNS):.1f}%   "
        f"← active threshold (MIN_PERSIST_RUNS={MIN_PERSIST_RUNS})"
    )
    logger.info(f"  % persisting ≥  5 runs:  {pct_ge(5):.1f}%")
    logger.info(f"  % persisting ≥ 10 runs:  {pct_ge(10):.1f}%")
    logger.info(f"  Chamber×layer groups with < {MIN_PERSIST_RUNS} total BSL rows (skipped in detection): {n_too_small}")

    if pct_at_threshold < 50.0:
        logger.warning(
            f"  *** CALIBRATION WARNING: Only {pct_at_threshold:.1f}% of step events persist "
            f">= {MIN_PERSIST_RUNS} runs.  MIN_PERSIST_RUNS may be too strict — "
            f"consider lowering it and re-running. ***"
        )
    elif pct_at_threshold > 95.0:
        logger.warning(
            f"  *** CALIBRATION WARNING: {pct_at_threshold:.1f}% of step events persist "
            f">= {MIN_PERSIST_RUNS} runs.  MIN_PERSIST_RUNS may be too permissive — "
            f"isolated spikes risk being treated as confirmed adjustments. ***"
        )
    else:
        logger.info(
            f"  Calibration OK — MIN_PERSIST_RUNS={MIN_PERSIST_RUNS} is appropriate for this dataset"
        )

    logger.info("=" * 70)


# ── Phase 2: Product target regime detection ───────────────────────────────────

def detect_target_regimes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per (PROD_MOP_PILOT, WEC_LAYER), detect step changes in ALLSTATS_MEAN_TARGET_VALUE
    (threshold: TARGET_CHANGE_THRESHOLD_NM).

    Adds three columns to df:
      TARGET_REGIME_LATEST  — bool, True for rows in the most recent target regime
      TARGET_CHANGE_DATE    — datetime of when the latest regime started (NaT if no change)
      N_PRE_CHANGE          — int, rows in all prior regimes combined
    """
    df = df.sort_values(
        ['PROD_MOP_PILOT', 'WEC_LAYER', 'DATA_COLLECTION_TIME']
    ).copy()

    regime_latest      = np.zeros(len(df), dtype=bool)
    target_change_date = pd.array([pd.NaT] * len(df), dtype='datetime64[ns]')
    n_pre_change       = np.zeros(len(df), dtype=int)

    idx_position = {label: pos for pos, label in enumerate(df.index)}

    for (_pilot, _layer), grp in df.groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER'], sort=False
    ):
        tgt   = grp['ALLSTATS_MEAN_TARGET_VALUE']
        diffs = tgt.diff().abs()
        change_labels = diffs[diffs >= TARGET_CHANGE_THRESHOLD_NM].index.tolist()

        positions = [idx_position[label] for label in grp.index]

        if not change_labels:
            # No target changes — entire group is the latest (and only) regime
            for pos in positions:
                regime_latest[pos] = True
        else:
            last_change_label = change_labels[-1]
            last_change_iloc  = grp.index.get_loc(last_change_label)
            # Rows at or after the last change are the latest regime
            latest_labels = grp.index[last_change_iloc:]
            pre_count     = last_change_iloc
            change_ts     = grp.at[last_change_label, 'DATA_COLLECTION_TIME']

            for label in grp.index:
                pos = idx_position[label]
                n_pre_change[pos] = pre_count
                target_change_date[pos] = change_ts

            for label in latest_labels:
                pos = idx_position[label]
                regime_latest[pos] = True

    df['TARGET_REGIME_LATEST'] = regime_latest
    df['TARGET_CHANGE_DATE']   = pd.array(target_change_date, dtype='datetime64[ns]')
    df['N_PRE_CHANGE']         = n_pre_change
    return df


# ── Phase 3: Chamber B_TOOL confirmed adjustment detection ────────────────────

def detect_btool_adjustments(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per (SUBENTITY, WEC_LAYER) on BSL-only rows with non-null APC_B_TOOL, find the
    most recent *confirmed* adjustment event.

    Note: This phase runs before MAD outlier tagging. B_TOOL regime detection uses
    all eligible APC_B_TOOL rows in the chamber/layer group and is not filtered by
    DTT MAD exclusion status.

    Confirmation requires:
      1. |APC_B_TOOL[i] − APC_B_TOOL[i-1]| >= B_TOOL_STEP_THRESHOLD
      2. The following MIN_PERSIST_RUNS BSL rows all remain within B_TOOL_STEP_THRESHOLD
         of the new level (no reversal or further large step in that window).

    Adds two columns keyed by (SUBENTITY, WEC_LAYER):
      LAST_BTOOL_ADJ_DATE  — datetime of the last confirmed adjustment (NaT if none)
      WINDOW_START_DATE    — LAST_BTOOL_ADJ_DATE if known, else earliest DATA_COLLECTION_TIME
                             in that chamber×layer group (full-range fallback)
    """
    bsl_btool = (
        df[df['APC_FB_SUC'].eq(1) & df['APC_B_TOOL'].notna()]
        .sort_values(['SUBENTITY', 'WEC_LAYER', 'DATA_COLLECTION_TIME'])
    )

    # Build per-group earliest date for the window-start fallback
    earliest_date = (
        df.groupby(['SUBENTITY', 'WEC_LAYER'])['DATA_COLLECTION_TIME']
        .min()
        .rename('EARLIEST_DATE')
        .reset_index()
    )

    adj_records: dict[tuple, pd.Timestamp] = {}

    for (subentity, layer), grp in bsl_btool.groupby(['SUBENTITY', 'WEC_LAYER'], sort=False):
        btool = grp['APC_B_TOOL'].to_numpy()
        times = grp['DATA_COLLECTION_TIME'].to_numpy()
        n     = len(btool)

        last_confirmed = pd.NaT

        for i in range(1, n):
            step_mag = abs(btool[i] - btool[i - 1])
            if step_mag < B_TOOL_STEP_THRESHOLD:
                continue

            # Need MIN_PERSIST_RUNS rows after position i
            end = i + 1 + MIN_PERSIST_RUNS
            if end > n:
                continue   # not enough subsequent rows to confirm

            new_level = btool[i]
            if all(
                abs(btool[j] - new_level) < B_TOOL_STEP_THRESHOLD
                for j in range(i + 1, end)
            ):
                last_confirmed = pd.Timestamp(times[i])

        adj_records[(subentity, layer)] = last_confirmed

    # Build lookup DataFrames and merge back — avoids slow row-wise apply
    adj_df = pd.DataFrame(
        [{'SUBENTITY': k[0], 'WEC_LAYER': k[1], 'LAST_BTOOL_ADJ_DATE': v}
         for k, v in adj_records.items()]
    )

    df = df.merge(adj_df, on=['SUBENTITY', 'WEC_LAYER'], how='left')
    df = df.merge(earliest_date, on=['SUBENTITY', 'WEC_LAYER'], how='left')

    df['WINDOW_START_DATE'] = df['LAST_BTOOL_ADJ_DATE'].fillna(df['EARLIEST_DATE'])
    df = df.drop(columns=['EARLIEST_DATE'])

    return df


# ── Phase 4: Chamber centering flags ──────────────────────────────────────────

def build_chamber_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per (SUBENTITY, WEC_LAYER), using BSL-only rows that fall within the
    post-adjustment assessment window:
      1. Apply MAD-based outlier suppression to STATISTICS_MEAN_DTT_VALUE.
      2. Compute 95% CI on the cleaned sample.
      3. Flag groups where the CI excludes zero; assign severity.

    Returns a DataFrame of flagged rows (one row per chamber x layer),
    and a detail DataFrame of every wafer row that entered the analysis
    (tagged with MAD_EXCLUDED and FLAG_PASSED flags for validation).
    """
    records     = []
    detail_rows = []
    bsl_df  = df[df['APC_FB_SUC'].eq(1)].copy()

    for (subentity, layer), grp in bsl_df.groupby(
        ['SUBENTITY', 'WEC_LAYER'], sort=False
    ):
        window_start = grp['WINDOW_START_DATE'].iloc[0]
        window_end   = grp['DATA_COLLECTION_TIME'].max()
        last_adj     = grp['LAST_BTOOL_ADJ_DATE'].iloc[0]

        in_window = (
            grp[grp['DATA_COLLECTION_TIME'] >= window_start]
            if not pd.isna(window_start)
            else grp
        )
        if in_window.empty:
            continue

        keep    = _keep_mask_after_mad(in_window, 'STATISTICS_MEAN_DTT_VALUE')
        n_excl  = int((~keep).sum())
        clean   = in_window[keep]

        n, mean, ci_lower, ci_upper = _centering_test(clean['STATISTICS_MEAN_DTT_VALUE'])
        flagged = _is_flagged(n, ci_lower, ci_upper)

        if not flagged:
            continue

        # Accumulate detail rows for flagged groups only
        _detail_chunk = in_window.copy()
        _detail_chunk['MAD_EXCLUDED'] = ~keep
        _detail_chunk['FLAG_PASSED']  = flagged
        detail_rows.append(_detail_chunk)

        notes = []
        if n_excl > 0:
            notes.append(f"{n_excl} outlier(s) excluded (MAD×{MAD_MULTIPLIER})")
        if pd.isna(last_adj):
            notes.append("no confirmed B_TOOL adjustment — full data range used as assessment window")

        # Most recent non-null APC_B_TOOL in the assessment window
        _btool_series = (
            in_window.sort_values('DATA_COLLECTION_TIME')['APC_B_TOOL'].dropna()
        )
        _current_btool = float(_btool_series.iloc[-1]) if len(_btool_series) > 0 else np.nan
        # Representative LAYER label (most recent in window)
        _layer_label = (
            in_window.sort_values('DATA_COLLECTION_TIME')['LAYER'].dropna().iloc[-1]
            if 'LAYER' in in_window.columns and in_window['LAYER'].notna().any()
            else ''
        )

        records.append({
            'FLAG_TYPE':            'CHAMBER_CENTERING',
            'LAYER_SHORT':          layer.split('_')[1] if '_' in layer else layer,
            'WEC_LAYER':            layer,
            'SUBENTITY':            subentity,
            'Metal Layers':         _layer_label,
            'DELTA(nm) NEEDED':     round(-mean, 4),
            'Current B_Tool':       _current_btool,
            'PROD_MOP_PILOT':       '',
            'APC_PRODGROUP':        'BSL (aggregated)',
            'N_WAFERS':             n,
            'MEAN_DTT_BIAS':        round(mean, 4),
            'CI_LOWER':             round(ci_lower, 4),
            'CI_UPPER':             round(ci_upper, 4),
            'LAST_BTOOL_ADJ_DATE':  last_adj,
            'WINDOW_START_DATE':    window_start,
            'WINDOW_END_DATE':      window_end,
            'N_OUTLIERS_EXCLUDED':  n_excl,
            'TARGET_CHANGE_DATE':   pd.NaT,
            'N_PRE_CHANGE':         np.nan,
            'NOTES':                '; '.join(notes),
        })

    detail_df = (
        pd.concat(detail_rows, ignore_index=True)
        if detail_rows else pd.DataFrame()
    )
    return pd.DataFrame(records), detail_df


# ── Phase 5: Product centering flags ──────────────────────────────────────────

def build_product_flags(
    df: pd.DataFrame,
    chamber_flagged_keys: set,
) -> pd.DataFrame:
    """
    Per (PROD_MOP_PILOT, WEC_LAYER, IS_BSL), using TARGET_REGIME_LATEST rows:
      1. Apply MAD-based outlier suppression to STATISTICS_MEAN_DTT_VALUE.
      2. Compute 95% CI on the cleaned fleet-aggregated sample.
      3. Flag groups where the CI excludes zero; assign severity.
      4. Detect chamber confound when >CONFOUND_DOMINANCE_THRESHOLD of the product's
         wafers ran on a single SUBENTITY that is also flagged as a chamber issue.

    Returns a DataFrame of flagged rows (one row per product×layer×BSL classification).
    """
    records   = []
    latest_df = df[df['TARGET_REGIME_LATEST']].copy()

    for (pilot, layer, is_bsl), grp in latest_df.groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER', 'IS_BSL'], sort=False
    ):
        keep   = _keep_mask_after_mad(grp, 'STATISTICS_MEAN_DTT_VALUE')
        n_excl = int((~keep).sum())
        clean  = grp[keep]

        n, mean, ci_lower, ci_upper = _centering_test(clean['STATISTICS_MEAN_DTT_VALUE'])
        if not _is_flagged(n, ci_lower, ci_upper):
            continue

        # Representative metadata
        tc_dates = grp['TARGET_CHANGE_DATE'].dropna()
        target_change_date = tc_dates.min() if len(tc_dates) > 0 else pd.NaT
        n_pre    = int(grp['N_PRE_CHANGE'].max())
        apc_pg   = (
            grp['APC_PRODGROUP'].dropna().mode().iloc[0]
            if grp['APC_PRODGROUP'].notna().any()
            else ''
        )

        notes = []
        if not is_bsl:
            notes.append('NPI — informational')
        if n_excl > 0:
            notes.append(f"{n_excl} outlier(s) excluded (MAD×{MAD_MULTIPLIER})")

        # Chamber confound check
        if 'SUBENTITY' in grp.columns and len(grp) > 0:
            sub_counts = grp['SUBENTITY'].value_counts()
            top_sub    = sub_counts.index[0]
            top_frac   = sub_counts.iloc[0] / len(grp)
            if (top_frac > CONFOUND_DOMINANCE_THRESHOLD
                    and (top_sub, layer) in chamber_flagged_keys):
                notes.append(
                    f"possible chamber confound: {top_sub} accounts for "
                    f"{top_frac:.0%} of wafers and is also a flagged chamber"
                )

        # Representative LAYER label for product flags
        _layer_label_prod = (
            clean['LAYER'].dropna().mode().iloc[0]
            if 'LAYER' in clean.columns and clean['LAYER'].notna().any()
            else ''
        )

        records.append({
            'FLAG_TYPE':            'PRODUCT_TARGET',
            'LAYER_SHORT':          layer.split('_')[1] if '_' in layer else layer,
            'WEC_LAYER':            layer,
            'SUBENTITY':            '',
            'Metal Layers':         _layer_label_prod,
            'DELTA(nm) NEEDED':     round(mean, 4),
            'Current B_Tool':       np.nan,
            'PROD_MOP_PILOT':       pilot,
            'APC_PRODGROUP':        apc_pg,
            'N_WAFERS':             n,
            'MEAN_DTT_BIAS':        round(mean, 4),
            'CI_LOWER':             round(ci_lower, 4),
            'CI_UPPER':             round(ci_upper, 4),
            'LAST_BTOOL_ADJ_DATE':  pd.NaT,
            'WINDOW_START_DATE':    grp['DATA_COLLECTION_TIME'].min(),
            'WINDOW_END_DATE':      grp['DATA_COLLECTION_TIME'].max(),
            'N_OUTLIERS_EXCLUDED':  n_excl,
            'TARGET_CHANGE_DATE':   target_change_date,
            'N_PRE_CHANGE':         n_pre if n_pre > 0 else np.nan,
            'NOTES':                '; '.join(notes),
        })

    return pd.DataFrame(records)


# ── Phase 6: Assemble and rank ─────────────────────────────────────────────────

def assemble_and_rank(
    chamber_flags: pd.DataFrame,
    product_flags: pd.DataFrame,
) -> pd.DataFrame:
    """
    Combine chamber and product flag tables.  Within each (FLAG_TYPE, WEC_LAYER)
    group, keep the top FLAGS_PER_LAYER entries ranked by |MEAN_DTT_BIAS| and
    assign PRIO 1-FLAGS_PER_LAYER (1 = largest absolute bias = most urgent).
    Output is sorted by FLAG_TYPE, WEC_LAYER, PRIO for easy reading.
    """
    combined = pd.concat([chamber_flags, product_flags], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    combined['_abs_bias'] = combined['MEAN_DTT_BIAS'].abs()

    ranked_parts = []
    for (flag_type, layer), grp in combined.groupby(['FLAG_TYPE', 'WEC_LAYER'], sort=False):
        top = (
            grp.sort_values('_abs_bias', ascending=False)
            .head(FLAGS_PER_LAYER)
            .copy()
        )
        top['PRIO'] = range(1, len(top) + 1)
        ranked_parts.append(top)

    if not ranked_parts:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    result = (
        pd.concat(ranked_parts, ignore_index=True)
        .drop(columns=['_abs_bias'])
        .sort_values(['FLAG_TYPE', 'WEC_LAYER', 'PRIO'])
        .reset_index(drop=True)
    )
    return result.reindex(columns=[c for c in _OUTPUT_COLUMNS if c in result.columns])
def run_flagging_engine(input_csv_path: str = None) -> pd.DataFrame:
    """
    Execute the full sDTT flagging engine (Phases 0-6).

    Parameters
    ----------
    input_csv_path : str, optional
        Path to the APC-enriched HCCD CSV.  Defaults to INPUT_CSV constant.

    Returns
    -------
    pd.DataFrame
        Prioritised flags table.  Also saved to:
        <input_csv_dir>/sDTT_flags_HCCD_D1V_<YYYYMMDD>.csv
        <input_csv_dir>/sDTT_chamber_flag_detail_<YYYYMMDD>.csv  (all in-window rows for validation)
    """
    start_time  = datetime.now()
    input_path  = Path(input_csv_path or INPUT_CSV)
    date_str    = datetime.now().strftime('%Y%m%d')
    output_path = input_path.parent / f"sDTT_flags_HCCD_D1V_{date_str}.csv"

    logger.info("=" * 70)
    logger.info("sDTT FLAGGING ENGINE — D1V HCCD")
    logger.info(f"Input:  {input_path}")
    logger.info(f"Output: {output_path}")
    logger.info("=" * 70)

    # ── Load & basic prep ─────────────────────────────────────────────────────
    logger.info("Loading input CSV...")
    df = pd.read_csv(input_path, low_memory=False)
    logger.info(f"  {len(df):,} rows × {len(df.columns)} columns loaded")

    df['DATA_COLLECTION_TIME']     = pd.to_datetime(
        df['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce'
    )
    df['APC_B_TOOL']               = pd.to_numeric(df['APC_B_TOOL'], errors='coerce')
    df['STATISTICS_MEAN_DTT_VALUE'] = pd.to_numeric(df['STATISTICS_MEAN_DTT_VALUE'], errors='coerce')
    df['ALLSTATS_MEAN_DTT_VALUE']   = pd.to_numeric(df['ALLSTATS_MEAN_DTT_VALUE'], errors='coerce')
    df['ALLSTATS_MEAN_TARGET_VALUE'] = pd.to_numeric(
        df['ALLSTATS_MEAN_TARGET_VALUE'], errors='coerce'
    )

    # Keep only DYNWAFER_001 rows — these are the single chart-point wafers
    # that match the SPC control-chart published values (same as JSL SPC mode filter)
    n_before = len(df)
    if 'ALLSTATS_DYNWAFER' in df.columns:
        df = df[df['ALLSTATS_DYNWAFER'] == 'DYNWAFER_001'].reset_index(drop=True)
        logger.info(
            f"  {n_before - len(df):,} rows dropped (ALLSTATS_DYNWAFER != DYNWAFER_001); "
            f"{len(df):,} remain"
        )
    else:
        logger.warning("  ALLSTATS_DYNWAFER column not found — DYNWAFER_001 filter skipped")

    # Row-level BSL classification based on APC_PRODGROUP at time of measurement
    df['IS_BSL'] = df['APC_PRODGROUP'].astype(str).str.startswith('BSL')

    # Drop rows with no SPC DTT measurement — cannot participate in any centering test
    n_before = len(df)
    df = df[df['STATISTICS_MEAN_DTT_VALUE'].notna()].reset_index(drop=True)
    logger.info(
        f"  {n_before - len(df):,} rows dropped (null STATISTICS_MEAN_DTT_VALUE); "
        f"{len(df):,} remain"
    )

    # Drop rows missing either SUBENTITY or PROD_MOP_PILOT — cannot be grouped
    required_str_cols = ['SUBENTITY', 'PROD_MOP_PILOT', 'WEC_LAYER']
    for col in required_str_cols:
        if col not in df.columns:
            logger.warning(f"  Required column '{col}' missing from input — column added as empty string")
            df[col] = ''
    n_before = len(df)
    df = df[df['SUBENTITY'].notna() & df['PROD_MOP_PILOT'].notna()].reset_index(drop=True)
    if len(df) < n_before:
        logger.info(f"  {n_before - len(df):,} additional rows dropped (null SUBENTITY or PROD_MOP_PILOT)")

    logger.info(
        f"  BSL rows: {df['IS_BSL'].sum():,} | NPI rows: {(~df['IS_BSL']).sum():,}"
    )

    # ── Phase 0 ───────────────────────────────────────────────────────────────
    run_btool_persistence_calibration(df)

    # ── Phase 2: Product target regime detection ──────────────────────────────
    if PRODUCT_FLAGS_ENABLED:
        logger.info("Phase 2 — Detecting product target change regimes...")
        df = detect_target_regimes(df)
        n_latest  = int(df['TARGET_REGIME_LATEST'].sum())
        n_changed = int((df['TARGET_REGIME_LATEST'] & df['TARGET_CHANGE_DATE'].notna()).sum())
        logger.info(
            f"  {n_latest:,} rows in latest target regime; "
            f"{n_changed:,} have a prior target-change event on record"
        )
    else:
        logger.info("Phase 2 — Skipped (PRODUCT_FLAGS_ENABLED = False)")

    # ── Phase 3: B_TOOL adjustment detection ─────────────────────────────────
    logger.info("Phase 3 — Detecting confirmed APC_B_TOOL chamber adjustments...")
    df = detect_btool_adjustments(df)
    bsl_groups   = df[df['IS_BSL']].groupby(['SUBENTITY', 'WEC_LAYER'])
    n_total_grps = bsl_groups.ngroups
    n_adj_grps   = int(
        bsl_groups['LAST_BTOOL_ADJ_DATE'].first().notna().sum()
    )
    logger.info(
        f"  {n_adj_grps} of {n_total_grps} BSL chamber×layer groups have a "
        f"confirmed B_TOOL adjustment in the dataset"
    )

    # ── Phase 4: Chamber centering flags ─────────────────────────────────────
    logger.info("Phase 4 — Computing chamber centering flags (APC_FB_SUC=1, post-adjustment window)...")
    chamber_flags, chamber_detail = build_chamber_flags(df)
    n_layers_ch = chamber_flags['WEC_LAYER'].nunique() if not chamber_flags.empty else 0
    logger.info(
        f"  {len(chamber_flags)} chamber×layer groups passed centering test "
        f"across {n_layers_ch} layer(s); top {FLAGS_PER_LAYER} per layer kept in Phase 6"
    )
    chamber_flagged_keys = (
        set(zip(chamber_flags['SUBENTITY'], chamber_flags['WEC_LAYER']))
        if not chamber_flags.empty else set()
    )

    # ── Phase 5: Product centering flags ─────────────────────────────────────
    if PRODUCT_FLAGS_ENABLED:
        logger.info("Phase 5 — Computing product centering flags (latest target regime, all chambers)...")
        product_flags = build_product_flags(df, chamber_flagged_keys)
        n_layers_pr = product_flags['WEC_LAYER'].nunique() if not product_flags.empty else 0
        logger.info(
            f"  {len(product_flags)} product×layer groups passed centering test "
            f"across {n_layers_pr} layer(s); top {FLAGS_PER_LAYER} per layer kept in Phase 6"
        )
    else:
        logger.info("Phase 5 — Skipped (PRODUCT_FLAGS_ENABLED = False)")
        product_flags = pd.DataFrame()

    # ── Phase 6: Assemble, rank, save ────────────────────────────────────────
    logger.info("Phase 6 — Assembling and ranking all flags...")
    flags_df = assemble_and_rank(chamber_flags, product_flags)

    # Save detail rows — only for the top-ranked groups that appear in final output
    detail_path = input_path.parent / f"sDTT_chamber_flag_detail_{date_str}.csv"
    if not chamber_detail.empty:
        final_ch = flags_df[flags_df['FLAG_TYPE'] == 'CHAMBER_CENTERING']
        final_keys = set(zip(final_ch['SUBENTITY'], final_ch['WEC_LAYER']))
        detail_filtered = chamber_detail[
            chamber_detail.apply(
                lambda r: (r['SUBENTITY'], r['WEC_LAYER']) in final_keys, axis=1
            )
        ]
        if not detail_filtered.empty:
            detail_filtered.to_csv(detail_path, index=False)
            n_excl = int(detail_filtered['MAD_EXCLUDED'].sum())
            logger.info(
                f"  Detail CSV: {len(detail_filtered)} in-window rows "
                f"({n_excl} MAD-excluded) across {len(final_keys)} ranked chamber×layer groups "
                f"→ {detail_path}"
            )

    # ── Phase 7: flag visualizations ──────────────────────────────────────────
    _sdtt_dir = Path(INPUT_CSV).parent.parent
    import sys as _sys
    if str(_sdtt_dir) not in _sys.path:
        _sys.path.insert(0, str(_sdtt_dir))
    try:
        from sDTT_flag_visualizer import generate_flag_visualizations as _gen_viz
        logger.info("Phase 7 — Generating flag visualizations...")
        _gen_viz(df, flags_df, chamber_detail, _sdtt_dir / "sDTT_flag_visualizer.py")
    except ImportError as _viz_err:
        logger.warning("Phase 7 — Visualizer not available: %s", _viz_err)

    elapsed = datetime.now() - start_time

    logger.info("=" * 70)
    logger.info("FLAGGING ENGINE COMPLETE")
    logger.info("=" * 70)
    logger.info(f"  Total flags:       {len(flags_df)}")
    for ft in ('CHAMBER_CENTERING', 'PRODUCT_TARGET'):
        if flags_df.empty:
            break
        sub = flags_df[flags_df['FLAG_TYPE'] == ft]
        if sub.empty:
            continue
        n_layers = sub['WEC_LAYER'].nunique()
        logger.info(
            f"  {ft}: {len(sub)} flags across {n_layers} layer(s) "
            f"(PRIO 1\u20135 within each layer)"
        )
    logger.info(f"  Processing time:   {elapsed}")
    logger.info(f"  Output file:       {output_path}")
    logger.info("=" * 70)

    if not flags_df.empty:
        flags_df.to_csv(output_path, index=False)
        logger.info("Output CSV saved.")
    else:
        logger.info("No flags generated — output file not written.")

    return flags_df


# ── Standalone execution ───────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        result = run_flagging_engine()
        if result.empty:
            print("\nNo actionable flags found in the dataset.")
        else:
            pd.set_option('display.max_columns', None)
            pd.set_option('display.width', 220)
            pd.set_option('display.max_colwidth', 60)
            print(f"\n{len(result)} flag(s) generated. Top 20:\n")
            print(result.head(20).to_string(index=False))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as exc:
        print(f"\nFlagging engine failed: {exc}")
        logger.error("Flagging engine failed", exc_info=True)
