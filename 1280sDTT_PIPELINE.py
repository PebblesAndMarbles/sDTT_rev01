# -*- coding: utf-8 -*-
"""
1280 sDTT Pipeline Orchestrator
================================
Generates all SPC/WEC CSVs (via 1280sDTT_D1V.py) for process node 1280,
D1V site only, layers M6–M16.  No APC join step.

Final CSVs land in ``integrated_output/`` so downstream JMP scripts have a
stable target distinct from the debug working area.

Usage
-----
  # Full run using config defaults:
  python 1280sDTT_PIPELINE.py

  # Override lookback window:
  python 1280sDTT_PIPELINE.py --days 5

  # Specific CD levels:
  python 1280sDTT_PIPELINE.py --cd-levels HCCD DCCD

  # Resume an interrupted run:
  python 1280sDTT_PIPELINE.py --resume

  # Initial backfill (120 days):
  python 1280sDTT_PIPELINE.py --days 120

Windows Task Scheduler (daily refresh, 06:00)
----------------------------------------------
  Program : C:\\Users\\tbatson\\My Programs\\SQLPathFinder3\\Python3\\python.exe
  Args    : //orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01/1280sDTT_PIPELINE.py --days 5
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
    'sites':      ['D1V'],                      # D1V only for 1280
    'cd_levels':  ['HCCD', 'DCCD', 'FCCD'],    # all three CD levels
    #'cd_levels': ['DCCD'],                     # DCCD only (faster test run)

    # ── Layer scope ───────────────────────────────────────────────────────
    'layerRange': [6, 16],   # M6–M16 inclusive
    'incBM0':     0,         # no BM0 for 1280

    # ── Lookback window ───────────────────────────────────────────────────
    'days':       5,   # nightly scheduled; use 120 for initial backfill

    # ── APC join — not applicable for 1280 ───────────────────────────────
    'skip_apc_join': True,   # always True; no APC in 1280 pipeline

    # ── Console verbosity ─────────────────────────────────────────────────
    # progress_only = True  → terminal shows only Site / Layer / Chunk / CD
    #                          progress lines (all detail still in log file)
    # progress_only = False → full verbose output in terminal
    'progress_only': True,

    # Choices: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
    'log_level':  'INFO',

    # ── Chunk size (lots per DB query) ────────────────────────────────────
    'nLots_chunk': 75,

    # ── Debug / intermediate CSV writes ──────────────────────────────────
    # True  = write all per-chunk intermediate CSVs to query_files folder
    # False = skip intermediate writes; only final output CSVs are written
    #         (recommended for production / scheduled runs)
    'debug_writes': False,

    # ── Resume a failed run ───────────────────────────────────────────────
    # False = normal start (clears any existing temp CSVs and starts fresh)
    # True  = pick up from the last completed layer using the checkpoint file
    #         Use --resume on the CLI instead of editing here.
    'resume': False,
}
# ══════════════════════════════════════════════════════════════════════════════


# ── Logging ───────────────────────────────────────────────────────────────────

class ProgressFilter(logging.Filter):
    """Console filter: passes only high-level progress lines and WARNING+.

    The log file always receives everything.
    """
    PROGRESS_KEYWORDS = (
        # ── pipeline banners ──────────────────────────────────────────────
        'sDTT PIPELINE',
        'STEP 1',
        'Pipeline log:',
        '1280 SDTT Data Processing Script',
        # ── site / layer / chunk progress ─────────────────────────────────
        'Processing Site',
        'Processing chunk',
        'Chunk ',
        'Processing ',
        'Layer ',
        'Finalizing SDTT',
        # ── CD level & save ───────────────────────────────────────────────
        'Processing CD level',
        'Found ',
        'Saving data for',
        'SPC/WEC CSVs saved',
        # ── Resume / checkpoint ───────────────────────────────────────────
        'Resuming ',
        '[RESUME]',
        'Checkpoint',
        'checkpoint',
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return any(kw in msg for kw in self.PROGRESS_KEYWORDS)


def setup_pipeline_logging(log_level: str = 'INFO',
                           progress_only: bool = False) -> logging.Logger:
    """Initialise logging for the pipeline orchestrator."""
    timestamp  = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file   = LOG_DIR / f'sdtt_1280_pipeline_{timestamp}.log'
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

    for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
                   'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    logger = logging.getLogger('sDTT_1280_PIPELINE')
    mode = 'progress-only' if progress_only else 'verbose'
    logger.info(f"Pipeline log ({mode}): {log_file}")
    return logger


# ── Module loader ─────────────────────────────────────────────────────────────
def _load_module(name: str, path: Path):
    """Load a Python file as a module via importlib."""
    spec   = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Argument parsing ──────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='1280 sDTT Pipeline Orchestrator — D1V, M6–M16, no APC',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--days', type=int, default=None,
        help="Lookback window in days (overrides PIPELINE_CONFIG default)")
    parser.add_argument(
        '--cd-levels', nargs='+', default=None,
        dest='cd_levels',
        help="CD levels to process, e.g. --cd-levels HCCD DCCD FCCD")
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
      1. CLI flags (--days, --cd-levels, --resume, --log-level)
      2. PIPELINE_CONFIG values defined at the top of this file
      3. base_config from the data module (fallback for any uncovered keys)
    """
    cfg = base_config.copy()

    # ── Apply PIPELINE_CONFIG defaults ────────────────────────────────────
    cfg['sites']         = PIPELINE_CONFIG['sites']
    cfg['cd_levels']     = PIPELINE_CONFIG['cd_levels']
    cfg['layerRange']    = PIPELINE_CONFIG['layerRange']
    cfg['incBM0']        = PIPELINE_CONFIG['incBM0']
    cfg['days']          = PIPELINE_CONFIG['days']
    cfg['nLots_chunk']   = PIPELINE_CONFIG['nLots_chunk']
    cfg['log_level']     = PIPELINE_CONFIG['log_level']
    cfg['skip_apc_join'] = PIPELINE_CONFIG['skip_apc_join']  # always True
    cfg['debug_writes']  = PIPELINE_CONFIG['debug_writes']
    cfg['resume']        = PIPELINE_CONFIG.get('resume', False)

    # ── Redirect ALL output paths to integrated_output ────────────────────
    cfg['main_csv_path'] = str(INTEGRATED_OUTPUT) + os.sep
    cfg['folder_path']   = str(QUERY_FILES_DIR) + os.sep

    # ── CLI overrides (highest priority) ──────────────────────────────────
    if args.days is not None:
        cfg['days'] = args.days
    if args.cd_levels is not None:
        cfg['cd_levels'] = args.cd_levels
    if args.resume:
        cfg['resume'] = True
    if args.log_level != 'INFO':
        cfg['log_level'] = args.log_level

    return cfg


# ── Main entry point ──────────────────────────────────────────────────────────
def main(argv=None) -> None:
    args          = parse_args(argv)
    progress_only = PIPELINE_CONFIG.get('progress_only', True)
    logger        = setup_pipeline_logging(args.log_level, progress_only=progress_only)

    logger.info("=" * 70)
    logger.info("1280 sDTT PIPELINE ORCHESTRATOR — START")
    logger.info("=" * 70)
    logger.info(f"Script directory : {SCRIPT_DIR}")
    logger.info(f"Integrated output: {INTEGRATED_OUTPUT}")
    logger.info(f"Arguments        : {vars(args)}")

    # ── Load the data-generating module ───────────────────────────────────
    d1v_path = SCRIPT_DIR / '1280sDTT_D1V.py'
    if not d1v_path.exists():
        logger.error(f"Data module not found: {d1v_path}")
        sys.exit(1)

    logger.info(f"Loading data module: {d1v_path}")
    d1v_mod = _load_module('_sdtt_1280_d1v', d1v_path)

    # Build effective config (redirects output to integrated_output)
    config = build_config(args, d1v_mod.CONFIG)
    logger.info(f"Effective config: {config}")

    # ── STEP 1: SPC/WEC CSV generation ────────────────────────────────────
    logger.info("=" * 70)
    logger.info("STEP 1: SPC/WEC CSV generation")
    logger.info(f"  Sites     : {config['sites']}")
    logger.info(f"  CD levels : {config['cd_levels']}")
    logger.info(f"  Layers    : M{config['layerRange'][0]}–M{config['layerRange'][1]}")
    logger.info(f"  Days      : {config['days']}")
    logger.info(f"  Output    : {config['main_csv_path']}")
    logger.info("=" * 70)

    try:
        d1v_mod.main(config)
        logger.info("STEP 1 complete — SPC/WEC CSVs saved.")
    except Exception as exc:
        logger.error(f"STEP 1 FAILED: {exc}", exc_info=True)
        logger.error("Pipeline aborted.  Existing integrated_output CSVs are unchanged.")
        try:
            del d1v_mod
        except NameError:
            pass
        gc.collect()
        gc.collect()
        sys.exit(1)
    finally:
        # Release the data module so Python.NET CLRObject wrappers (PyUber
        # connection/cursor) are finalized before Shutdown() fires.
        try:
            del d1v_mod
        except NameError:
            pass
        gc.collect()
        gc.collect()

    logger.info("=" * 70)
    logger.info("1280 sDTT PIPELINE ORCHESTRATOR — COMPLETE")
    logger.info("=" * 70)

    gc.collect()
    gc.collect()


if __name__ == "__main__":
    main()
