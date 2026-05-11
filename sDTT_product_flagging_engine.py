import numpy as np
import pandas as pd
import logging
from datetime import datetime
from pathlib import Path
from sDTT_utils import _mad, _centering_test, _is_flagged, _keep_mask_after_mad

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Configuration constants ────────────────────────────────────────────────────

HOME_FACTORY_CSV = (
    r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson"
    r"\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_D1V_APC.csv"
)

SISTER_FACTORY_CSV = (
    r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson"
    r"\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_F32.csv"
)

# Centering flag thresholds
CI_CONFIDENCE   = 0.95
FLAGS_PER_LAYER = 5    # top-N flags kept per (FLAG_TYPE, WEC_LAYER); ranked PRIO 1-N
MIN_FLAG_N      = 5    # minimum wafers in assessment window to produce a flag

# Outlier suppression
MAD_MULTIPLIER = 3.0   # |value − median| > MAD_MULTIPLIER × MAD → treated as outlier

# Volume filter — qualifies a (PROD_MOP_PILOT, WEC_LAYER) group for analysis
LOOKBACK_DAYS   = 7    # rolling window for recent-run count
VOLUME_MIN_RUNS = 15   # group must have STRICTLY MORE than this many runs in the window

# Column aliases for lithography sub-group analysis
RETICLE_COL = 'ALLSTATS_CURRENT_RETICLE'
SCANNER_COL = 'SCANNER'

# ── Output column order ────────────────────────────────────────────────────────

_OUTPUT_COLUMNS = [
    'PRIO', 'FLAG_TYPE', 'LAYER_SHORT', 'WEC_LAYER',
    'PROD_MOP_PILOT', 'N_WAFERS',
    'MEAN_DTT_BIAS', 'DELTA(nm) NEEDED', 'CI_LOWER', 'CI_UPPER',
    'WINDOW_START_DATE', 'WINDOW_END_DATE',
    'N_OUTLIERS_EXCLUDED', 'TARGET_CHANGE_DATE', 'N_PRE_CHANGE',
    'FACTORY', 'RETICLE_ID', 'SCANNER_ID',
    'Metal Layers', 'NOTES',
]


# ── Phase 1: Multi-source data loading and harmonisation ──────────────────────

def load_and_harmonise(home_csv: str, sister_csv: str = None) -> pd.DataFrame:
    """
    Load the home-factory (D1V) CSV and, optionally, the sister-factory (F32)
    CSV.  Outputs a single harmonised DataFrame with a FACTORY column added.

    Applies the same preprocessing used by the chamber engine:
      - ALLSTATS_DYNWAFER == 'DYNWAFER_001' filter (single chart-point wafers)
      - Drop rows with null STATISTICS_MEAN_DTT_VALUE
      - Parse DATA_COLLECTION_TIME to datetime
      - Coerce numeric assessment and target columns

    The F32 CSV has no APC_* columns — this engine is entirely APC-free so
    their absence causes no issues.
    """
    frames = []
    for path, label in [(home_csv, 'D1V'), (sister_csv, 'F32')]:
        if not path:
            continue
        logger.info(f"  Loading {label}: {path}")
        _df = pd.read_csv(path, low_memory=False)
        _df['FACTORY'] = label
        logger.info(f"    {len(_df):,} rows × {len(_df.columns)} columns ({label})")
        frames.append(_df)

    if not frames:
        raise ValueError("No input CSV paths provided.")

    df = pd.concat(frames, ignore_index=True)

    # ── Parse / coerce ─────────────────────────────────────────────────────────
    df['DATA_COLLECTION_TIME'] = pd.to_datetime(
        df['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce'
    )
    df['STATISTICS_MEAN_DTT_VALUE'] = pd.to_numeric(
        df['STATISTICS_MEAN_DTT_VALUE'], errors='coerce'
    )
    df['ALLSTATS_MEAN_TARGET_VALUE'] = pd.to_numeric(
        df['ALLSTATS_MEAN_TARGET_VALUE'], errors='coerce'
    )

    # ── DYNWAFER_001 filter ────────────────────────────────────────────────────
    n_before = len(df)
    if 'ALLSTATS_DYNWAFER' in df.columns:
        df = df[df['ALLSTATS_DYNWAFER'] == 'DYNWAFER_001'].reset_index(drop=True)
        logger.info(
            f"  {n_before - len(df):,} rows dropped (ALLSTATS_DYNWAFER != DYNWAFER_001); "
            f"{len(df):,} remain"
        )
    else:
        logger.warning("  ALLSTATS_DYNWAFER column not found — DYNWAFER_001 filter skipped")

    # ── Drop rows with no DTT measurement ─────────────────────────────────────
    n_before = len(df)
    df = df[df['STATISTICS_MEAN_DTT_VALUE'].notna()].reset_index(drop=True)
    logger.info(
        f"  {n_before - len(df):,} rows dropped (null STATISTICS_MEAN_DTT_VALUE); "
        f"{len(df):,} remain"
    )

    # ── Ensure required grouping columns exist ─────────────────────────────────
    for col in ['PROD_MOP_PILOT', 'WEC_LAYER']:
        if col not in df.columns:
            logger.warning(f"  Required column '{col}' missing — added as empty string")
            df[col] = ''
    df = df[df['PROD_MOP_PILOT'].notna() & df['WEC_LAYER'].notna()].reset_index(drop=True)

    # ── Factory composition summary ────────────────────────────────────────────
    for fac, cnt in df['FACTORY'].value_counts().items():
        logger.info(f"  {fac}: {cnt:,} rows after filtering")

    return df


# ── Phase 2: Discrete product target regime detection ─────────────────────────

def detect_target_regimes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per (PROD_MOP_PILOT, WEC_LAYER), identify the current target regime as the
    unbroken trailing run of the most recently observed ALLSTATS_MEAN_TARGET_VALUE.

    Unlike the threshold-based version in the chamber engine, ANY change in the
    target value — regardless of magnitude — defines a new regime boundary.
    The relevant population for flagging is only the rows that ran at the current
    (most recent) target value continuously through to the end of the dataset.

    Adds three columns:
      TARGET_REGIME_LATEST  — bool, True for rows in the trailing (current) regime
      TARGET_CHANGE_DATE    — datetime of the first row of the current regime
                              (NaT when the entire history has a single target)
      N_PRE_CHANGE          — int, rows that preceded the current regime (0 if none)
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
        tgt       = grp['ALLSTATS_MEAN_TARGET_VALUE'].to_numpy()
        times     = grp['DATA_COLLECTION_TIME'].to_numpy()
        positions = [idx_position[label] for label in grp.index]
        last_val  = tgt[-1]

        # If the last target value is NaN we cannot define a meaningful regime;
        # fall back to treating the entire group as the current regime.
        if np.isnan(last_val):
            for pos in positions:
                regime_latest[pos] = True
            continue

        # Walk backwards to find the start of the continuous trailing run
        # of last_val (NaN in mid-series acts as an implicit boundary).
        regime_start_iloc = len(tgt) - 1
        while (
            regime_start_iloc > 0
            and not np.isnan(tgt[regime_start_iloc - 1])
            and tgt[regime_start_iloc - 1] == last_val
        ):
            regime_start_iloc -= 1

        for i, pos in enumerate(positions):
            if i >= regime_start_iloc:
                regime_latest[pos] = True

        if regime_start_iloc > 0:
            change_ts = pd.Timestamp(times[regime_start_iloc])
            for pos in positions:
                target_change_date[pos] = change_ts
                n_pre_change[pos]       = regime_start_iloc

    df['TARGET_REGIME_LATEST'] = regime_latest
    df['TARGET_CHANGE_DATE']   = pd.array(target_change_date, dtype='datetime64[ns]')
    df['N_PRE_CHANGE']         = n_pre_change
    return df


# ── Phase 3: Volume-qualified group selection ─────────────────────────────────

def apply_volume_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Within TARGET_REGIME_LATEST rows, mark each (PROD_MOP_PILOT, WEC_LAYER)
    group as VOLUME_QUALIFIED if it has STRICTLY MORE than VOLUME_MIN_RUNS rows
    whose DATA_COLLECTION_TIME falls within the past LOOKBACK_DAYS days
    (measured from the group's most recent observation).

    Groups that do not meet the threshold are excluded from Phases 4 and 5.
    This replaces the BSL / NPI distinction used in the chamber engine.

    Adds column:
      VOLUME_QUALIFIED — bool
    """
    latest = df[df['TARGET_REGIME_LATEST']].copy()

    qual_records = []
    for (pilot, layer), grp in latest.groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER'], sort=False
    ):
        ref_time = grp['DATA_COLLECTION_TIME'].max()
        cutoff   = ref_time - pd.Timedelta(days=LOOKBACK_DAYS)
        recent_n = int((grp['DATA_COLLECTION_TIME'] >= cutoff).sum())
        qual_records.append({
            'PROD_MOP_PILOT':  pilot,
            'WEC_LAYER':       layer,
            'VOLUME_QUALIFIED': recent_n > VOLUME_MIN_RUNS,
        })

    qual_df = pd.DataFrame(qual_records)
    df = df.merge(qual_df, on=['PROD_MOP_PILOT', 'WEC_LAYER'], how='left')
    df['VOLUME_QUALIFIED'] = df['VOLUME_QUALIFIED'].fillna(False)
    return df


# ── Phase 4: Product centering flags ──────────────────────────────────────────

def build_product_flags(df: pd.DataFrame) -> tuple:
    """
    Per VOLUME_QUALIFIED (PROD_MOP_PILOT, WEC_LAYER) group in the current
    target regime:
      1. Apply MAD-based outlier suppression to STATISTICS_MEAN_DTT_VALUE.
      2. Compute 95% CI on the cleaned sample.
      3. Flag groups where the CI excludes zero.

    When both D1V and F32 rows are present in the same group, the factory
    composition is noted and the flag is labelled COMBINED.

    Returns a (flags DataFrame, detail DataFrame) tuple.  The detail DataFrame
    tags every qualifying wafer row with MAD_EXCLUDED, FLAG_PASSED, and FACTORY
    for downstream validation and visualisation.
    """
    records     = []
    detail_rows = []

    qualified = df[df['TARGET_REGIME_LATEST'] & df['VOLUME_QUALIFIED']].copy()

    for (pilot, layer), grp in qualified.groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER'], sort=False
    ):
        keep   = _keep_mask_after_mad(grp, 'STATISTICS_MEAN_DTT_VALUE', MAD_MULTIPLIER)
        n_excl = int((~keep).sum())
        clean  = grp[keep]

        n, mean, ci_lower, ci_upper = _centering_test(
            clean['STATISTICS_MEAN_DTT_VALUE'], CI_CONFIDENCE
        )
        if not _is_flagged(n, ci_lower, ci_upper, MIN_FLAG_N):
            continue

        # Per-wafer detail for flagged groups
        _chunk = grp.copy()
        _chunk['MAD_EXCLUDED'] = ~keep
        _chunk['FLAG_PASSED']  = True
        detail_rows.append(_chunk)

        notes = []
        if n_excl > 0:
            notes.append(f"{n_excl} outlier(s) excluded (MAD×{MAD_MULTIPLIER})")

        # Factory composition
        factory_counts = grp['FACTORY'].value_counts()
        if len(factory_counts) > 1:
            factory_label = 'COMBINED'
            parts = ', '.join(f"{k}:{int(v)}" for k, v in factory_counts.items())
            notes.append(f"combined factories ({parts})")
        else:
            factory_label = factory_counts.index[0]

        tc_dates           = grp['TARGET_CHANGE_DATE'].dropna()
        target_change_date = tc_dates.min() if len(tc_dates) > 0 else pd.NaT
        n_pre              = int(grp['N_PRE_CHANGE'].max())

        _layer_label = (
            clean['LAYER'].dropna().mode().iloc[0]
            if 'LAYER' in clean.columns and clean['LAYER'].notna().any()
            else ''
        )

        records.append({
            'FLAG_TYPE':           'PRODUCT_TARGET',
            'LAYER_SHORT':         layer.split('_')[1] if '_' in layer else layer,
            'WEC_LAYER':           layer,
            'PROD_MOP_PILOT':      pilot,
            'N_WAFERS':            n,
            'MEAN_DTT_BIAS':       round(mean, 4),
            'DELTA(nm) NEEDED':    round(mean, 4),
            'CI_LOWER':            round(ci_lower, 4),
            'CI_UPPER':            round(ci_upper, 4),
            'WINDOW_START_DATE':   grp['DATA_COLLECTION_TIME'].min(),
            'WINDOW_END_DATE':     grp['DATA_COLLECTION_TIME'].max(),
            'N_OUTLIERS_EXCLUDED': n_excl,
            'TARGET_CHANGE_DATE':  target_change_date,
            'N_PRE_CHANGE':        n_pre if n_pre > 0 else np.nan,
            'FACTORY':             factory_label,
            'RETICLE_ID':          '',
            'SCANNER_ID':          '',
            'Metal Layers':        _layer_label,
            'NOTES':               '; '.join(notes),
        })

    detail_df = (
        pd.concat(detail_rows, ignore_index=True)
        if detail_rows else pd.DataFrame()
    )
    return pd.DataFrame(records), detail_df


# ── Phase 5: Reticle and scanner sub-group flags ──────────────────────────────

def build_subgroup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Within each VOLUME_QUALIFIED (PROD_MOP_PILOT, WEC_LAYER) group, run the
    same centering test independently for each lithography sub-group:

      ALLSTATS_CURRENT_RETICLE → FLAG_TYPE = 'PRODUCT_RETICLE'
      SCANNER                  → FLAG_TYPE = 'PRODUCT_SCANNER'

    Sub-group entries with fewer than MIN_FLAG_N rows or where the CI straddles
    zero are silently skipped.  NaN / blank sub-group labels are always skipped.

    Output rows populate either RETICLE_ID or SCANNER_ID as appropriate;
    the other identity column is left as an empty string.
    """
    records   = []
    qualified = df[df['TARGET_REGIME_LATEST'] & df['VOLUME_QUALIFIED']].copy()

    # Each tuple: (source column, FLAG_TYPE, column to populate, column to leave empty)
    dimensions = [
        (RETICLE_COL, 'PRODUCT_RETICLE', 'RETICLE_ID', 'SCANNER_ID'),
        (SCANNER_COL,  'PRODUCT_SCANNER', 'SCANNER_ID', 'RETICLE_ID'),
    ]

    for (pilot, layer), grp in qualified.groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER'], sort=False
    ):
        for dim_col, flag_type, populated_col, empty_col in dimensions:
            if dim_col not in grp.columns:
                continue

            for sub_val, sub_grp in grp.groupby(dim_col, sort=False):
                if pd.isna(sub_val) or str(sub_val).strip() == '':
                    continue

                keep   = _keep_mask_after_mad(
                    sub_grp, 'STATISTICS_MEAN_DTT_VALUE', MAD_MULTIPLIER
                )
                n_excl = int((~keep).sum())
                clean  = sub_grp[keep]

                n, mean, ci_lower, ci_upper = _centering_test(
                    clean['STATISTICS_MEAN_DTT_VALUE'], CI_CONFIDENCE
                )
                if not _is_flagged(n, ci_lower, ci_upper, MIN_FLAG_N):
                    continue

                notes = []
                if n_excl > 0:
                    notes.append(f"{n_excl} outlier(s) excluded (MAD×{MAD_MULTIPLIER})")

                factory_counts = sub_grp['FACTORY'].value_counts()
                factory_label  = (
                    'COMBINED' if len(factory_counts) > 1
                    else factory_counts.index[0]
                )

                tc_val = (
                    sub_grp['TARGET_CHANGE_DATE'].dropna().min()
                    if sub_grp['TARGET_CHANGE_DATE'].notna().any()
                    else pd.NaT
                )

                records.append({
                    'FLAG_TYPE':           flag_type,
                    'LAYER_SHORT':         layer.split('_')[1] if '_' in layer else layer,
                    'WEC_LAYER':           layer,
                    'PROD_MOP_PILOT':      pilot,
                    'N_WAFERS':            n,
                    'MEAN_DTT_BIAS':       round(mean, 4),
                    'DELTA(nm) NEEDED':    round(mean, 4),
                    'CI_LOWER':            round(ci_lower, 4),
                    'CI_UPPER':            round(ci_upper, 4),
                    'WINDOW_START_DATE':   sub_grp['DATA_COLLECTION_TIME'].min(),
                    'WINDOW_END_DATE':     sub_grp['DATA_COLLECTION_TIME'].max(),
                    'N_OUTLIERS_EXCLUDED': n_excl,
                    'TARGET_CHANGE_DATE':  tc_val,
                    'N_PRE_CHANGE':        np.nan,
                    'FACTORY':             factory_label,
                    populated_col:         str(sub_val),
                    empty_col:             '',
                    'Metal Layers':        '',
                    'NOTES':               '; '.join(notes),
                })

    return pd.DataFrame(records)


# ── Phase 6: Assemble and rank ─────────────────────────────────────────────────

def assemble_and_rank(
    product_flags: pd.DataFrame,
    subgroup_flags: pd.DataFrame,
) -> pd.DataFrame:
    """
    Combine product-level and sub-group flag tables.  Within each
    (FLAG_TYPE, WEC_LAYER) group, keep the top FLAGS_PER_LAYER entries
    ranked by |MEAN_DTT_BIAS| and assign PRIO 1-FLAGS_PER_LAYER (1 = most urgent).
    Output is sorted by FLAG_TYPE, WEC_LAYER, PRIO.
    """
    combined = pd.concat([product_flags, subgroup_flags], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    combined['_abs_bias'] = combined['MEAN_DTT_BIAS'].abs()

    ranked_parts = []
    for (flag_type, layer), grp in combined.groupby(
        ['FLAG_TYPE', 'WEC_LAYER'], sort=False
    ):
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


# ── Main orchestrator ──────────────────────────────────────────────────────────

def run_product_flagging_engine(
    home_csv_path: str = None,
    sister_csv_path: str = None,
) -> pd.DataFrame:
    """
    Execute the sDTT product target flagging engine (Phases 1–6).

    Parameters
    ----------
    home_csv_path : str, optional
        Path to the home-factory HCCD CSV.  Defaults to HOME_FACTORY_CSV.
    sister_csv_path : str, optional
        Path to the sister-factory HCCD CSV.  Defaults to SISTER_FACTORY_CSV.
        Pass an empty string to skip sister-factory data.

    Returns
    -------
    pd.DataFrame
        Prioritised product flags table.  Also saved to:
          <home_csv_dir>/sDTT_product_flags_<YYYYMMDD>.csv
          <home_csv_dir>/sDTT_product_flag_detail_<YYYYMMDD>.csv
    """
    start_time  = datetime.now()
    home_path   = Path(home_csv_path or HOME_FACTORY_CSV)
    _sister_raw = sister_csv_path if sister_csv_path is not None else SISTER_FACTORY_CSV
    sister_path = Path(_sister_raw) if _sister_raw else None
    date_str    = datetime.now().strftime('%Y%m%d')
    output_dir  = home_path.parent
    output_path = output_dir / f"sDTT_product_flags_{date_str}.csv"
    detail_path = output_dir / f"sDTT_product_flag_detail_{date_str}.csv"

    logger.info("=" * 70)
    logger.info("sDTT PRODUCT FLAGGING ENGINE")
    logger.info(f"Home factory:    {home_path}")
    logger.info(f"Sister factory:  {sister_path or '(not loaded)'}")
    logger.info(f"Output:          {output_path}")
    logger.info("=" * 70)

    # ── Phase 1: Load & harmonise ─────────────────────────────────────────────
    logger.info("Phase 1 — Loading and harmonising source data...")
    df = load_and_harmonise(
        home_csv=str(home_path),
        sister_csv=str(sister_path) if sister_path else None,
    )
    logger.info(f"  Combined dataset: {len(df):,} rows")

    # ── Phase 2: Discrete target regime detection ─────────────────────────────
    logger.info("Phase 2 — Detecting discrete product target change regimes...")
    df = detect_target_regimes(df)
    n_latest  = int(df['TARGET_REGIME_LATEST'].sum())
    n_changed = int((df['TARGET_REGIME_LATEST'] & df['TARGET_CHANGE_DATE'].notna()).sum())
    logger.info(
        f"  {n_latest:,} rows in current target regime; "
        f"{n_changed:,} have a prior target-change on record"
    )

    # ── Phase 3: Volume filter ────────────────────────────────────────────────
    logger.info(
        f"Phase 3 — Applying volume filter "
        f"(>{VOLUME_MIN_RUNS} runs in past {LOOKBACK_DAYS} days on current regime)..."
    )
    df = apply_volume_filter(df)
    latest_groups = df[df['TARGET_REGIME_LATEST']].groupby(
        ['PROD_MOP_PILOT', 'WEC_LAYER']
    )
    n_total = latest_groups.ngroups
    n_qual  = int(latest_groups.first()['VOLUME_QUALIFIED'].sum())
    logger.info(
        f"  {n_qual} of {n_total} product×layer groups qualify "
        f"({n_total - n_qual} below volume threshold)"
    )

    # ── Phase 4: Product centering flags ─────────────────────────────────────
    logger.info("Phase 4 — Computing product centering flags...")
    product_flags, detail_df = build_product_flags(df)
    n_layers_p = product_flags['WEC_LAYER'].nunique() if not product_flags.empty else 0
    logger.info(
        f"  {len(product_flags)} product×layer group(s) flagged "
        f"across {n_layers_p} layer(s)"
    )

    # ── Phase 5: Reticle / scanner sub-group flags ────────────────────────────
    logger.info("Phase 5 — Computing reticle and scanner sub-group flags...")
    subgroup_flags = build_subgroup_flags(df)
    n_reticle = int((subgroup_flags['FLAG_TYPE'] == 'PRODUCT_RETICLE').sum()) if not subgroup_flags.empty else 0
    n_scanner = int((subgroup_flags['FLAG_TYPE'] == 'PRODUCT_SCANNER').sum()) if not subgroup_flags.empty else 0
    logger.info(f"  {n_reticle} PRODUCT_RETICLE flag(s);  {n_scanner} PRODUCT_SCANNER flag(s)")

    # ── Phase 6: Assemble and rank ────────────────────────────────────────────
    logger.info("Phase 6 — Assembling and ranking all product flags...")
    flags_df = assemble_and_rank(product_flags, subgroup_flags)

    # ── Save outputs ───────────────────────────────────────────────────────────
    if not flags_df.empty:
        flags_df.to_csv(output_path, index=False)
        logger.info(f"  Flags CSV saved → {output_path}")
    else:
        logger.info("  No product flags generated — flags CSV not written.")

    if not detail_df.empty:
        final_keys = (
            set(zip(flags_df['PROD_MOP_PILOT'], flags_df['WEC_LAYER']))
            if not flags_df.empty else set()
        )
        detail_filtered = detail_df[
            detail_df.apply(
                lambda r: (r['PROD_MOP_PILOT'], r['WEC_LAYER']) in final_keys, axis=1
            )
        ]
        if not detail_filtered.empty:
            detail_filtered.to_csv(detail_path, index=False)
            n_excl_d = int(detail_filtered['MAD_EXCLUDED'].sum())
            logger.info(
                f"  Detail CSV: {len(detail_filtered)} rows "
                f"({n_excl_d} MAD-excluded) → {detail_path}"
            )

    elapsed = datetime.now() - start_time

    logger.info("=" * 70)
    logger.info("PRODUCT FLAGGING ENGINE COMPLETE")
    logger.info("=" * 70)
    logger.info(f"  Total flags:     {len(flags_df)}")
    for ft in ('PRODUCT_TARGET', 'PRODUCT_RETICLE', 'PRODUCT_SCANNER'):
        if flags_df.empty:
            break
        sub = flags_df[flags_df['FLAG_TYPE'] == ft]
        if sub.empty:
            continue
        logger.info(
            f"  {ft}: {len(sub)} flag(s) across "
            f"{sub['WEC_LAYER'].nunique()} layer(s)"
        )
    logger.info(f"  Processing time: {elapsed}")
    logger.info("=" * 70)

    return flags_df


# ── Standalone execution ───────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        result = run_product_flagging_engine()
        if result.empty:
            print("\nNo actionable product flags found in the dataset.")
        else:
            pd.set_option('display.max_columns', None)
            pd.set_option('display.width', 220)
            pd.set_option('display.max_colwidth', 60)
            print(f"\n{len(result)} flag(s) generated. Top 20:\n")
            print(result.head(20).to_string(index=False))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as exc:
        print(f"\nProduct flagging engine failed: {exc}")
        logger.error("Product flagging engine failed", exc_info=True)
