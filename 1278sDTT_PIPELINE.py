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
import sys
from datetime import datetime
from pathlib import Path

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

    # ── APC join (D1V + HCCD only) ────────────────────────────────────────
    'skip_apc_join': False,   # set True to save SPC/WEC CSVs only, skip APC

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
def parse_args() -> argparse.Namespace:
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
        '--log-level', default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging verbosity (default: INFO)')
    parser.add_argument(
        '--resume', action='store_true',
        help='Resume from last checkpoint; skip already-completed layers')
    return parser.parse_args()


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

    return cfg


# ── Main entry point ──────────────────────────────────────────────────────────
def main() -> None:
    args         = parse_args()
    progress_only = PIPELINE_CONFIG.get('progress_only', True)
    logger       = setup_pipeline_logging(args.log_level, progress_only=progress_only)

    logger.info("=" * 70)
    logger.info("sDTT PIPELINE ORCHESTRATOR — START")
    logger.info("=" * 70)
    logger.info(f"Script directory : {SCRIPT_DIR}")
    logger.info(f"Integrated output: {INTEGRATED_OUTPUT}")
    logger.info(f"Arguments        : {vars(args)}")

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

    # ── STEP 2: APC join — D1V + HCCD only ───────────────────────────────
    # NOTE: when running a full pipeline (not --apc-only), the APC join is
    # already triggered inside finalize_site_data() in the generating script
    # (unless --skip-apc was passed).  The explicit step below fires only in
    # --apc-only mode so that the join can be re-run on an existing CSV without
    # re-querying all SPC/WEC data.
    if args.apc_only and not args.skip_apc:
        if 'D1V' in config['sites'] and 'HCCD' in config['cd_levels']:
            logger.info("=" * 70)
            logger.info("STEP 2 (apc-only): APC join for D1V + HCCD")
            logger.info("=" * 70)

            base     = config['main_csv_base_name']
            apc_csv  = INTEGRATED_OUTPUT / f"{base}_HCCD_D1V.csv"

            if not apc_csv.exists():
                logger.warning(
                    f"APC join skipped — HCCD_D1V CSV not found in integrated_output: {apc_csv}\n"
                    f"  Run without --apc-only first (or check that STEP 1 completed successfully)."
                )
            else:
                apc_path = SCRIPT_DIR / '1278sDTT_D1V_HCCD_APC_JOIN.py'
                if not apc_path.exists():
                    logger.error(f"APC join script not found: {apc_path}")
                else:
                    try:
                        apc_mod = _load_module('_sdtt_apc_join', apc_path)
                        out = apc_mod.run_apc_join(str(apc_csv))
                        logger.info(f"STEP 2 complete — APC-enriched CSV: {out}")
                    except Exception as exc:
                        logger.error(f"STEP 2 FAILED (APC join): {exc}", exc_info=True)
                    finally:
                        try:
                            del apc_mod
                        except NameError:
                            pass
                        gc.collect()
                        gc.collect()
        else:
            logger.info("STEP 2 skipped — D1V+HCCD not in scope.")

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
