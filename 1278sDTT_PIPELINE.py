# -*- coding: utf-8 -*-
"""
sDTT Full Pipeline Orchestrator
================================
Generates all SPC/WEC CSVs (via 1278sDTT_D1V_F32.py) and then runs APC join
for D1V+HCCD.  Final CSVs land in the ``integrated_output`` folder so that
downstream JMP scripts have a stable, separate target distinct from the debug
working area.

Usage
-----
  # Full run (SPC/WEC generation + APC join), use config defaults:
  python 1278sDTT_PIPELINE.py

  # Override lookback window:
  python 1278sDTT_PIPELINE.py --days 5

  # Single site only:
  python 1278sDTT_PIPELINE.py --sites D1V

  # Specific CD levels:
  python 1278sDTT_PIPELINE.py --cd-levels HCCD DCCD

  # Skip the APC join step (saves plain SPC/WEC CSV only):
  python 1278sDTT_PIPELINE.py --skip-apc

  # Skip SPC/WEC generation; re-run APC join only on whatever CSV already exists:
  python 1278sDTT_PIPELINE.py --apc-only

Windows Task Scheduler (daily refresh, 06:00)
----------------------------------------------
  Program : C:\\Users\\tbatson\\My Programs\\SQLPathFinder3\\Python3\\python.exe
  Args    : //orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01/1278sDTT_PIPELINE.py --days 5
"""

import argparse
import gc
import importlib.util
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Path setup ────────────────────────────────────────────────────────────────
# Use os.path.abspath rather than Path.resolve() — on Windows, resolve() can
# prepend '\\?\UNC\' to network UNC paths, which breaks some Windows APIs and
# causes FileNotFoundError when pandas writes CSVs on those paths.
SCRIPT_DIR        = Path(os.path.abspath(__file__)).parent
INTEGRATED_OUTPUT = SCRIPT_DIR / 'integrated_output'
QUERY_FILES_DIR   = INTEGRATED_OUTPUT / 'query_files'   # intermediate per-chunk CSVs
LOG_DIR           = SCRIPT_DIR / 'logs'

# Ensure output directories exist
INTEGRATED_OUTPUT.mkdir(exist_ok=True)
QUERY_FILES_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Add script directory to sys.path so sub-modules can be imported
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE CONFIGURATION — edit these values before running
# ══════════════════════════════════════════════════════════════════════════════
PIPELINE_CONFIG = {
    # ── Sites & scope ─────────────────────────────────────────────────────
    'sites':      ['F32', 'D1V'],       # both sites
    #'sites':      ['F32'],              # F32 only
    'cd_levels':  ['HCCD','DCCD','FCCD'],  # all three CD levels
    #'cd_levels': ['HCCD'],             # HCCD only (faster test run)

    # ── Layer scope ───────────────────────────────────────────────────────
    'layerRange': [5, 14],  # [first_layer, last_layer] inclusive
    'incBM0':     1,        # 1 = include BM0 layer, 0 = exclude

    # ── Lookback window ───────────────────────────────────────────────────
    'days':       5,   # nightly scheduled; use 120 for initial backfill

    # ── APC join (D1V + F32 + HCCD) ──────────────────────────────────────────
    'skip_apc_join': False,   # set True to save SPC/WEC CSVs only, skip APC
    # Limits APC DB query scope to recent source rows as a fallback safety net
    # when the wafer manifest is unavailable (e.g. first-run or manifest write fail).
    # Incremental/manifest mode is preferred and ignores this when manifest is valid.
    # Set to None to disable the limit (query all wafers, not recommended for nightly runs).
    'apc_query_lookback_days': 10,
    # Operations that require strict AREA+B_TOOL pre-query before accepting APC match.
    # MT5H (op 263067) needs this to avoid false empty matches from cascading fallback.
    'require_area_btool_for_match_ops': ['263067'],
    # Enforce strict AREA+B_TOOL qualification specifically at FLOW_TEMP tier
    # for all operations before accepting a tier match.
    'require_area_btool_for_flow_temp': True,
    # PM-safe filtering for split-chamber lot APC rows (prevents PM-token flipping).
    # LOCKED IN: Always enabled to ensure SUBENTITY consistency across all pipeline runs.
    'use_subentity_pm_match': True,

    # ── Console verbosity ──────────────────────────────────────────────
    # progress_only = True  → terminal shows only Site / Layer / Chunk / CD
    #                          progress lines (all detail still in log file)
    # progress_only = False → full verbose output in terminal**
    'progress_only': True,

    # Choices: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
    'log_level':  'INFO',

    # ── Chunk size (lots per DB query) ────────────────────────────────────
    'nLots_chunk': 75,

    # ── Debug / intermediate CSV writes ──────────────────────────────────
    # True  = write all per-chunk intermediate CSVs to query_files folder
    #         (useful for debugging; ~1 400 UNC writes per full D1V run)
    # False = skip intermediate writes; only final output CSVs are written
    #         (recommended for production / scheduled runs — noticeably faster)
    'debug_writes': False,

    # ── Resume a failed run ───────────────────────────────────────────────
    # False = normal start (clears any existing temp CSVs and starts fresh)
    # True  = pick up from the last completed layer using the checkpoint file
    #         in query_files/.  Use --resume on the CLI instead of editing here.
    'resume': False,
}
# ══════════════════════════════════════════════════════════════════════════════


# ── Logging ───────────────────────────────────────────────────────────────────

class ProgressFilter(logging.Filter):
    """Console filter: passes only high-level progress lines and all
    WARNING/ERROR/CRITICAL records.  Full detail is always written to the
    log file (which has no filter attached).

    A message is passed when it matches any of the PROGRESS_KEYWORDS or when
    its level is WARNING or above.
    """
    PROGRESS_KEYWORDS = (
        # ── pipeline banners ───────────────────────────────────
        'sDTT PIPELINE',
        'STEP 1',
        'STEP 2',
        'Pipeline log:',
        'SDTT Data Processing Script',
        # ── site / layer / chunk progress ────────────────────────
        'Processing Site',      # site banner + layer header
        'Processing chunk',     # chunk N of M
        'Chunk ',               # "Chunk N completed"
        'Processing ',          # "Processing N lots in M chunks"
        'Layer ',               # "Layer N processing complete"
        'Finalizing SDTT',
        # ── CD level & save ──────────────────────────────────
        'Processing CD level',
        'Found ',               # "Found N records for CD level"
        'Saving data for',
        'SPC/WEC CSVs saved',
        # ── APC ──────────────────────────────────────────────
        'APC join',
        'Starting APC join',
        'APC join completed',
        'Saving results to',
        'CASCADING AREA PRIORITY',
        # ── Resume / checkpoint ───────────────────────────────
        'Resuming ',
        '[RESUME]',
        'Checkpoint',
        'checkpoint',
    )

    def filter(self, record: logging.LogRecord) -> bool:
        # Always show warnings and above
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return any(kw in msg for kw in self.PROGRESS_KEYWORDS)


def setup_pipeline_logging(log_level: str = 'INFO',
                           progress_only: bool = False) -> logging.Logger:
    """Initialise logging for the pipeline orchestrator.

    Parameters
    ----------
    log_level     : root log level (applies to both file and console handlers)
    progress_only : when True, attach ProgressFilter to the console handler so
                    only high-level progress lines appear in the terminal;
                    the log file always receives everything.
    """
    timestamp  = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file   = LOG_DIR / f'sdtt_pipeline_{timestamp}.log'
    fmt        = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    file_handler    = logging.FileHandler(log_file, encoding='utf-8')
    console_handler = logging.StreamHandler(sys.stdout)
    if progress_only:
        console_handler.addFilter(ProgressFilter())

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=fmt,
        handlers=[file_handler, console_handler],
    )

    # Silence third-party loggers that echo SQL or internal plumbing at INFO.
    # pd.read_sql via a non-SQLAlchemy connection (PyUber) causes sqlalchemy.engine
    # to log the full SQL text at INFO; PyUber has its own chatty logger too.
    for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
                   'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    logger = logging.getLogger('sDTT_PIPELINE')
    mode = 'progress-only' if progress_only else 'verbose'
    logger.info(f"Pipeline log ({mode}): {log_file}")
    return logger


# ── Module loader ─────────────────────────────────────────────────────────────
def _load_module(name: str, path: Path):
    """Load a Python file as a module via importlib (no package restructure needed)."""
    spec   = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Argument parsing ──────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='sDTT Full Pipeline Orchestrator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--days', type=int, default=None,
        help='Lookback window in days (overrides CONFIG default of 120)')
    parser.add_argument(
        '--sites', nargs='+', default=None,
        help="Sites to process, e.g. --sites D1V F32  (default: CONFIG['sites'])")
    parser.add_argument(
        '--cd-levels', nargs='+', default=None,
        dest='cd_levels',
        help="CD levels to process, e.g. --cd-levels HCCD DCCD FCCD")
    parser.add_argument(
        '--skip-apc', action='store_true',
        help='Skip the APC join step; save SPC/WEC CSV only')
    parser.add_argument(
        '--apc-only', action='store_true',
        help='Skip SPC/WEC generation; re-run APC join on the existing HCCD_D1V CSV')
    parser.add_argument(
        '--fill-null-col', default=None, metavar='COL', dest='fill_null_col',
        help='Used with --apc-only: only re-query wafers where COL is null in the '
             'existing _APC.csv (e.g. --fill-null-col APC_B_TOOL). '
             'Non-null rows are retained as-is, saving significant query time.')
    parser.add_argument(
        '--apc-debug-minimal', action='store_true', dest='apc_debug_minimal',
        help='Used with --apc-only: run minimal APC debug query (AREA + B_TOOL only) '
             'against recent source rows and write a separate debug APC CSV.')
    parser.add_argument(
           '--apc-debug-days', type=int, default=3, dest='apc_debug_days',
        help='Used with --apc-debug-minimal: source-row lookback window in days '
               '(default: 3).')
    parser.add_argument(
        '--apc-lookback-days', type=int, default=None, dest='apc_lookback_days',
        help='Used with --apc-only (or APC step): limit APC DB querying to rows '
             'whose DATA_COLLECTION_TIME is within the last N days, while keeping '
             'the full source CSV in output.')
    parser.add_argument(
        '--apc-query-key-manifest', type=str, default=None, dest='apc_query_key_manifest',
        help='Used with --apc-only: path to key-manifest CSV (WAFER_ID and optional '
             'WEC_OPERATION) for targeted APC re-query.')
    parser.add_argument(
        '--apc-output-mode', choices=['full', 'patch'], default='full', dest='apc_output_mode',
        help="Used with --apc-only: APC output mode. 'full' writes standard _APC CSV; "
             "'patch' writes sidecar patch rows for later merge.")
    parser.add_argument(
        '--apc-patch-output-dir', type=str, default=None, dest='apc_patch_output_dir',
        help='Used with --apc-only and --apc-output-mode patch: output directory for '
             'site-specific APC patch CSVs.')
    parser.add_argument(
        '--apc-query-batch-id', type=str, default=None, dest='apc_query_batch_id',
        help='Optional batch identifier propagated into APC patch output metadata.')
    parser.add_argument(
        '--use-subentity-pm-match', action='store_true', dest='use_subentity_pm_match',
        help='Enable PM-safe APC filtering (source SUBENTITY PM must match APC SUBENTITY PM).')
    parser.add_argument(
        '--apc-replace-mt5h', action='store_true', dest='apc_replace_mt5h',
        help='Used with --apc-only: run strict APC logic for MT5H only and replace '
             'corresponding MT5H rows in the production _APC.csv without full backfill.')
    parser.add_argument(
        '--mt5h-operation', type=str, default='263067', dest='mt5h_operation',
        help='Operation used for MT5H replacement mode (default: 263067).')
    parser.add_argument(
        '--mt5h-test-name', type=str, default='8CD.FCCD.MT5H', dest='mt5h_test_name',
        help='TEST_NAME scope guard used in MT5H replacement mode (default: 8CD.FCCD.MT5H).')
    parser.add_argument(
        '--mt5h-layer', type=str, default='MT5', dest='mt5h_layer',
        help='LAYER scope guard used in MT5H replacement mode (default: MT5).')
    parser.add_argument(
        '--log-level', default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging verbosity (default: INFO)')
    parser.add_argument(
        '--resume', action='store_true',
        help='Resume from last checkpoint; skip already-completed layers')
    return parser.parse_args(argv)


# ── Build effective config ────────────────────────────────────────────────────
def build_config(args: argparse.Namespace, base_config: dict) -> dict:
    """Build the effective config for this pipeline run.

    Priority (highest → lowest):
      1. CLI flags (--days, --sites, --cd-levels, --skip-apc)
      2. PIPELINE_CONFIG values defined at the top of this file
      3. base_config from F32 script (used for keys not covered above)
    """
    cfg = base_config.copy()

    # ── Apply PIPELINE_CONFIG defaults ────────────────────────────────────
    cfg['sites']        = PIPELINE_CONFIG['sites']
    cfg['cd_levels']    = PIPELINE_CONFIG['cd_levels']
    cfg['layerRange']   = PIPELINE_CONFIG['layerRange']
    cfg['incBM0']       = PIPELINE_CONFIG['incBM0']
    cfg['days']         = PIPELINE_CONFIG['days']
    cfg['nLots_chunk']  = PIPELINE_CONFIG['nLots_chunk']
    cfg['log_level']    = PIPELINE_CONFIG['log_level']
    cfg['skip_apc_join']  = PIPELINE_CONFIG['skip_apc_join']
    cfg['debug_writes'] = PIPELINE_CONFIG['debug_writes']
    cfg['resume']       = PIPELINE_CONFIG.get('resume', False)
    cfg['apc_query_lookback_days'] = PIPELINE_CONFIG.get('apc_query_lookback_days', None)
    cfg['require_area_btool_for_match_ops'] = PIPELINE_CONFIG.get('require_area_btool_for_match_ops', None)
    cfg['require_area_btool_for_flow_temp'] = PIPELINE_CONFIG.get('require_area_btool_for_flow_temp', True)
    cfg['use_subentity_pm_match'] = PIPELINE_CONFIG.get('use_subentity_pm_match', False)

    # ── Redirect ALL output paths to integrated_output ────────────────────
    # main_csv_path : final per-CD-level CSVs  (e.g. 1278sDTT_HCCD_D1V.csv)
    # folder_path   : intermediate per-chunk query CSVs
    cfg['main_csv_path'] = str(INTEGRATED_OUTPUT) + os.sep
    cfg['folder_path']   = str(QUERY_FILES_DIR) + os.sep

    # ── CLI overrides (highest priority) ──────────────────────────────────
    if args.days is not None:
        cfg['days'] = args.days
    if args.sites is not None:
        cfg['sites'] = args.sites
    if args.cd_levels is not None:
        cfg['cd_levels'] = args.cd_levels
    if args.skip_apc:
        cfg['skip_apc_join'] = True
    if args.resume:
        cfg['resume'] = True
    if args.log_level != 'INFO':          # non-default means user explicitly set it
        cfg['log_level'] = args.log_level
    if args.apc_lookback_days is not None:
        cfg['apc_query_lookback_days'] = args.apc_lookback_days
    if args.use_subentity_pm_match:
        cfg['use_subentity_pm_match'] = True

    return cfg


def _null_like_mask(series: pd.Series) -> pd.Series:
    null_vals = {'', '[NULL]', 'nan', 'None', 'NULL'}
    return series.isna() | series.astype(str).str.strip().isin(null_vals)


def _safe_rate(numer: int, denom: int) -> float:
    return (100.0 * float(numer) / float(denom)) if denom else 0.0


def _build_mt5h_scope_mask(df: pd.DataFrame, operation: str, test_name: str, layer: str) -> pd.Series:
    op_mask = pd.Series(True, index=df.index)
    if 'WEC_OPERATION' in df.columns:
        op_mask = df['WEC_OPERATION'].astype(str) == str(operation)

    test_mask = pd.Series(True, index=df.index)
    if 'TEST_NAME' in df.columns:
        test_mask = df['TEST_NAME'].astype(str) == str(test_name)

    layer_mask = pd.Series(True, index=df.index)
    if 'LAYER' in df.columns:
        layer_mask = df['LAYER'].astype(str) == str(layer)

    return op_mask & test_mask & layer_mask


def _run_mt5h_replacement_mode(
    apc_mod,
    base_hccd_csv: Path,
    production_apc_csv: Path,
    operation: str,
    test_name: str,
    layer: str,
    logger: logging.Logger,
    site: str = 'D1V',
) -> Path:
    """Generate strict MT5H APC rows and replace MT5H slice in production APC CSV."""
    if not base_hccd_csv.exists():
        raise FileNotFoundError(f"Base source CSV not found: {base_hccd_csv}")
    if not production_apc_csv.exists():
        raise FileNotFoundError(f"Production APC CSV not found: {production_apc_csv}")

    logger.info('MT5H replace mode: reading base HCCD source CSV')
    df_source = pd.read_csv(base_hccd_csv)
    source_scope = _build_mt5h_scope_mask(df_source, operation, test_name, layer)
    df_mt5h_source = df_source[source_scope].copy()
    logger.info(f"MT5H replace mode: source scope rows = {len(df_mt5h_source):,} of {len(df_source):,}")

    if df_mt5h_source.empty:
        raise RuntimeError('MT5H replace mode found zero rows in source CSV; aborting replacement')

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    mt5h_input = INTEGRATED_OUTPUT / f"{base_hccd_csv.stem}_MT5H_STRICT_INPUT_{stamp}{base_hccd_csv.suffix}"
    logger.info(f"MT5H replace mode: writing strict input slice: {mt5h_input}")
    df_mt5h_source.to_csv(mt5h_input, index=False)

    logger.info('MT5H replace mode: running strict APC join for MT5H operation only')
    mt5h_apc_out = apc_mod.run_apc_join(
        str(mt5h_input),
        require_area_btool_for_match_ops=[str(operation)],
        site=site,
    )
    if not mt5h_apc_out:
        raise RuntimeError('MT5H replace mode APC join returned empty output path')

    mt5h_apc_out_path = Path(mt5h_apc_out)
    if not mt5h_apc_out_path.exists():
        raise FileNotFoundError(f"MT5H replacement APC output missing: {mt5h_apc_out_path}")

    logger.info(f"MT5H replace mode: reading strict APC output: {mt5h_apc_out_path}")
    df_mt5h_new = pd.read_csv(mt5h_apc_out_path)
    new_scope = _build_mt5h_scope_mask(df_mt5h_new, operation, test_name, layer)
    df_mt5h_new = df_mt5h_new[new_scope].copy()

    if df_mt5h_new.empty:
        raise RuntimeError('MT5H replace mode strict APC output has zero MT5H rows after scope guard')

    missing_key_cols = [c for c in ['WAFER_ID', 'WEC_OPERATION'] if c not in df_mt5h_new.columns]
    if missing_key_cols:
        raise RuntimeError(f"MT5H replacement output missing key columns: {missing_key_cols}")

    key_dupes = df_mt5h_new.duplicated(subset=['WAFER_ID', 'WEC_OPERATION']).sum()
    if key_dupes > 0:
        logger.warning(f"MT5H strict output had {key_dupes} duplicate key rows; keeping last by key")
        df_mt5h_new = df_mt5h_new.drop_duplicates(subset=['WAFER_ID', 'WEC_OPERATION'], keep='last')

    logger.info(f"MT5H replace mode: reading production APC CSV: {production_apc_csv}")
    df_prod = pd.read_csv(production_apc_csv)

    prod_scope = _build_mt5h_scope_mask(df_prod, operation, test_name, layer)
    df_prod_mt5h = df_prod[prod_scope].copy()
    df_prod_other = df_prod[~prod_scope].copy()

    if 'APC_B_TOOL' in df_prod.columns:
        old_null = int(_null_like_mask(df_prod_mt5h['APC_B_TOOL']).sum()) if len(df_prod_mt5h) else 0
        old_rate = _safe_rate(old_null, len(df_prod_mt5h))
        logger.info(f"MT5H baseline APC_B_TOOL nulls: {old_null}/{len(df_prod_mt5h)} ({old_rate:.2f}%)")

    backup_path = production_apc_csv.with_name(
        f"{production_apc_csv.stem}.bak_mt5h_replace_{stamp}{production_apc_csv.suffix}"
    )
    shutil.copy2(production_apc_csv, backup_path)
    logger.info(f"MT5H replace mode: backup created: {backup_path}")

    # Keep exact production schema/order; fill any missing columns from new slice.
    for col in df_prod.columns:
        if col not in df_mt5h_new.columns:
            df_mt5h_new[col] = pd.NA
    df_mt5h_new = df_mt5h_new[df_prod.columns]

    df_final = pd.concat([df_prod_other, df_mt5h_new], ignore_index=True)
    if 'DATA_COLLECTION_TIME' in df_final.columns:
        _ts = pd.to_datetime(df_final['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')
        df_final = df_final.assign(_ts_sort=_ts).sort_values('_ts_sort', ascending=False).drop(columns=['_ts_sort'])
        df_final.reset_index(drop=True, inplace=True)

    if len(df_final) != (len(df_prod_other) + len(df_mt5h_new)):
        raise RuntimeError('MT5H replace mode produced unexpected final row count')

    df_final.to_csv(production_apc_csv, index=False)

    if 'APC_B_TOOL' in df_final.columns:
        final_scope = _build_mt5h_scope_mask(df_final, operation, test_name, layer)
        new_null = int(_null_like_mask(df_final.loc[final_scope, 'APC_B_TOOL']).sum()) if final_scope.any() else 0
        new_cnt = int(final_scope.sum())
        new_rate = _safe_rate(new_null, new_cnt)
        logger.info(f"MT5H post-replace APC_B_TOOL nulls: {new_null}/{new_cnt} ({new_rate:.2f}%)")

    logger.info(
        f"MT5H replace mode complete: removed {len(df_prod_mt5h):,} old MT5H rows, "
        f"inserted {len(df_mt5h_new):,} strict MT5H rows"
    )
    return production_apc_csv


# ── Main entry point ──────────────────────────────────────────────────────────
def main(argv=None) -> None:
    args         = parse_args(argv)
    progress_only = PIPELINE_CONFIG.get('progress_only', True)
    logger       = setup_pipeline_logging(args.log_level, progress_only=progress_only)

    logger.info("=" * 70)
    logger.info("sDTT PIPELINE ORCHESTRATOR — START")
    logger.info("=" * 70)
    logger.info(f"Script directory : {SCRIPT_DIR}")
    logger.info(f"Integrated output: {INTEGRATED_OUTPUT}")
    logger.info(f"Arguments        : {vars(args)}")

    if args.apc_replace_mt5h and not args.apc_only:
        logger.warning(
            "--apc-replace-mt5h is intended for --apc-only mode; "
            "flag will be ignored unless --apc-only is also set."
        )

    # ── Load the generating script as a module ─────────────────────────────
    f32_path = SCRIPT_DIR / '1278sDTT_D1V_F32.py'
    if not f32_path.exists():
        logger.error(f"Generating script not found: {f32_path}")
        sys.exit(1)

    logger.info(f"Loading generating script: {f32_path}")
    f32_mod = _load_module('_sdtt_f32', f32_path)

    # Build effective config (redirects output to integrated_output)
    config = build_config(args, f32_mod.CONFIG)

    logger.info(f"Effective config: {config}")

    # ── STEP 1: SPC/WEC CSV generation ────────────────────────────────────
    if not args.apc_only:
        logger.info("=" * 70)
        logger.info("STEP 1: SPC/WEC CSV generation")
        logger.info(f"  Sites     : {config['sites']}")
        logger.info(f"  CD levels : {config['cd_levels']}")
        logger.info(f"  Days      : {config['days']}")
        logger.info(f"  Output    : {config['main_csv_path']}")
        logger.info("=" * 70)
        try:
            f32_mod.main(config)
            logger.info("STEP 1 complete — SPC/WEC CSVs saved.")
        except Exception as exc:
            logger.error(f"STEP 1 FAILED: {exc}", exc_info=True)
            logger.error("Pipeline aborted.  Existing integrated_output CSVs are unchanged.")
            # Release module reference before exit so Python.NET finalizers run cleanly
            del f32_mod
            gc.collect(); gc.collect()
            sys.exit(1)
        finally:
            # Release the generating-script module so Python.NET CLRObject wrappers
            # (PyUber connection/cursor) are finalized NOW, before Shutdown() fires.
            # Two GC passes ensure any objects freed in pass 1 have their own
            # finalizers run in pass 2.
            try:
                del f32_mod
            except NameError:
                pass
            gc.collect()
            gc.collect()
    else:
        logger.info("Skipping STEP 1 (--apc-only).")


    # ── STEP 2: APC join — D1V + HCCD and F32 + HCCD ───────────────────────────────
    # NOTE: when running a full pipeline (not --apc-only), the APC join is
    # already triggered inside finalize_site_data() in the generating script
    # (unless --skip-apc was passed).  The explicit step below fires only in
    # --apc-only mode so that the join can be re-run on an existing CSV without
    # re-querying all SPC/WEC data.
    if args.apc_only and not args.skip_apc:
        did_any = False
        for site in config['sites']:
            if site in ('D1V', 'F32') and 'HCCD' in config['cd_levels']:
                logger.info("=" * 70)
                logger.info(f"STEP 2 (apc-only): APC join for {site} + HCCD")
                logger.info("=" * 70)

                base = config.get('main_csv_base_name', f"1278sDTT")
                apc_csv = INTEGRATED_OUTPUT / f"{base}_HCCD_{site}.csv"

                if not apc_csv.exists():
                    logger.warning(
                        f"APC join skipped — HCCD_{site} CSV not found in integrated_output: {apc_csv}\n"
                        f"  Run without --apc-only first (or check that STEP 1 completed successfully)."
                    )
                    continue

                apc_path = SCRIPT_DIR / '1278sDTT_D1V_HCCD_APC_JOIN.py'
                if not apc_path.exists():
                    logger.error(f"APC join script not found: {apc_path}")
                    continue

                try:
                    apc_mod = _load_module('_sdtt_apc_join', apc_path)
                    if args.apc_replace_mt5h:
                        out = _run_mt5h_replacement_mode(
                            apc_mod=apc_mod,
                            base_hccd_csv=apc_csv,
                            production_apc_csv=INTEGRATED_OUTPUT / f"{base}_HCCD_{site}_APC.csv",
                            operation=str(args.mt5h_operation),
                            test_name=args.mt5h_test_name,
                            layer=args.mt5h_layer,
                            logger=logger,
                            site=site,
                        )
                    else:
                        _patch_output = None
                        if args.apc_output_mode == 'patch' and args.apc_patch_output_dir:
                            _patch_dir = Path(args.apc_patch_output_dir)
                            _patch_dir.mkdir(parents=True, exist_ok=True)
                            _batch = args.apc_query_batch_id or datetime.now().strftime('%Y%m%d_%H%M%S')
                            _patch_output = str(_patch_dir / f"{base}_HCCD_{site}_APC_PATCH_{_batch}.csv")

                        out = apc_mod.run_apc_join(
                            str(apc_csv),
                            query_key_manifest_path=args.apc_query_key_manifest,
                            output_mode=args.apc_output_mode,
                            patch_output_path=_patch_output,
                            query_batch_id=args.apc_query_batch_id,
                            fill_null_col=args.fill_null_col,
                            apc_debug_minimal=args.apc_debug_minimal,
                            debug_days=args.apc_debug_days,
                            apc_query_lookback_days=args.apc_lookback_days,
                            site=site,
                            require_area_btool_for_match_ops=config.get('require_area_btool_for_match_ops'),
                            require_area_btool_for_flow_temp=config.get('require_area_btool_for_flow_temp', True),
                            use_subentity_pm_match=config.get('use_subentity_pm_match', False),
                        )
                    logger.info(f"STEP 2 complete — APC-enriched CSV: {out}")
                    did_any = True
                except Exception as exc:
                    logger.error(f"STEP 2 FAILED (APC join) for {site}: {exc}", exc_info=True)
                finally:
                    try:
                        del apc_mod
                    except NameError:
                        pass
                    gc.collect()
                    gc.collect()
        if not did_any:
            logger.info("STEP 2 skipped — no eligible site+HCCD in scope.")

    elif args.skip_apc:
        logger.info("STEP 2 skipped (--skip-apc).")
    else:
        logger.info("STEP 2 was handled inline by finalize_site_data() during STEP 1.")

    logger.info("=" * 70)
    logger.info("sDTT PIPELINE ORCHESTRATOR — COMPLETE")
    logger.info("=" * 70)

    # Final GC pass — by this point all module references (f32_mod, apc_mod) have
    # already been deleted in the finally blocks above, so any residual Python.NET
    # CLRObject wrappers should already be unreachable.  Two passes here as a
    # safety net in case any weak-ref cycles survived.
    gc.collect()
    gc.collect()


if __name__ == "__main__":
    main()
