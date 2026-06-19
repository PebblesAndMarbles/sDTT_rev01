# -*- coding: utf-8 -*-
"""
1278 NCD MT5+MT6 D1V Pipeline Orchestrator
==========================================
Thin orchestrator for the standalone NCD measurement pipeline.
Mirrors the structure of 1278sDTT_PIPELINE.py.

Usage
-----
  # Run with config default (3-day lookback):
  python 1278sDTT_NCD_PIPELINE.py

  # Override lookback window:
  python 1278sDTT_NCD_PIPELINE.py --days 7

Windows Task Scheduler
----------------------
  Program : C:\\Users\\tbatson\\My Programs\\SQLPathFinder3\\Python3\\python.exe
  Args    : //orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01/1278sDTT_NCD_PIPELINE.py --days 3
"""

import argparse
import importlib.util
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR        = Path(os.path.abspath(__file__)).parent
INTEGRATED_OUTPUT = SCRIPT_DIR / 'integrated_output'
LOG_DIR           = SCRIPT_DIR / 'logs'

INTEGRATED_OUTPUT.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
PIPELINE_CONFIG = {
    # ── Site & scope ──────────────────────────────────────────────────────
    'sites':      ['D1V'],          # D1V only

    # ── Lookback window ───────────────────────────────────────────────────
    'days':       3,                # nightly scheduled; use 30 for backfill

    # ── Output ────────────────────────────────────────────────────────────
    'output_csv_name': '1278sDTT_NCD_MT6_D1V',  # retained name; now includes MT5+MT6

    # ── Console verbosity ─────────────────────────────────────────────────
    'progress_only': True,

    # Choices: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
    'log_level':  'INFO',

    # ── Chunk size ────────────────────────────────────────────────────────
    'nLots_chunk': 25,

    # ── Debug intermediate CSV writes ─────────────────────────────────────
    'debug_writes': False,

    # ── Internal paths (resolved relative to script dir) ─────────────────
    'folder_path': str(SCRIPT_DIR / 'debug' / '1278 QUERY FILES') + os.sep,
    'main_csv_path': str(INTEGRATED_OUTPUT) + os.sep,

    # ── Fixed constants (not user-configurable) ───────────────────────────
    'tech': '1278',
    'tech_alias_nums': {'1278': '8'},
    'database_connections': {'D1V': 'D1D_PROD_XEUS_LOCAL'},
    'main_csv_base_name': '1278sDTT',
    'suppress_sqlalchemy_warnings': True,
}
# ══════════════════════════════════════════════════════════════════════════════


class ProgressFilter(logging.Filter):
    """Console filter: passes only pipeline-level progress banners and WARNING+."""
    KEYWORDS = (
        'NCD MT5+MT6', 'STEP ', 'PROBE RESULT', 'OUTPUT:', 'COMPLETE',
        'Processing chunk', 'Chunk ', 'lots found', 'SPCS ID',
    )

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return any(kw in msg for kw in self.KEYWORDS)


class SafeConsoleHandler(logging.StreamHandler):
    """Console handler that degrades non-encodable chars instead of crashing."""

    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                enc = getattr(self.stream, 'encoding', None) or 'cp1252'
                safe = msg.encode(enc, errors='replace').decode(enc, errors='replace')
                self.stream.write(safe + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)


def setup_pipeline_logging(log_level, progress_only):
    """Configure pipeline-level logging with optional progress-only console."""
    root = logging.getLogger()
    root.handlers.clear()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path  = LOG_DIR / f'sdtt_ncd_pipeline_{timestamp}.log'

    fmt = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    )

    # File handler — always full verbosity
    fh = logging.FileHandler(str(log_path), encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — optionally filtered
    ch = SafeConsoleHandler(sys.stdout)
    ch.setLevel(getattr(logging, log_level.upper()))
    ch.setFormatter(fmt)
    if progress_only:
        ch.addFilter(ProgressFilter())
    root.addHandler(ch)

    root.setLevel(logging.DEBUG)

    for noisy in ('sqlalchemy.engine', 'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger('NCD_PIPELINE'), str(log_path)


def load_ncd_module():
    """Dynamically load 1278sDTT_NCD_D1V.py from the same directory."""
    mod_path = SCRIPT_DIR / '1278sDTT_NCD_D1V.py'
    if not mod_path.exists():
        raise FileNotFoundError(f'NCD module not found: {mod_path}')
    spec = importlib.util.spec_from_file_location('sdtt_ncd_d1v', str(mod_path))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_args():
    p = argparse.ArgumentParser(description='1278 NCD MT5+MT6 D1V Pipeline')
    p.add_argument('--days', type=int, default=None,
                   help='Lookback window in days (overrides PIPELINE_CONFIG)')
    p.add_argument('--debug-writes', action='store_true',
                   help='Write intermediate debug CSVs to query_files folder')
    p.add_argument('--verbose', action='store_true',
                   help='Full console output (disables progress_only filter)')
    return p.parse_args()


def main():
    args = parse_args()

    config = dict(PIPELINE_CONFIG)
    if args.days is not None:
        config['days'] = args.days
    if args.debug_writes:
        config['debug_writes'] = True
    if args.verbose:
        config['progress_only'] = False

    logger, log_path = setup_pipeline_logging(
        config['log_level'], config['progress_only']
    )

    logger.info('=' * 80)
    logger.info('1278 NCD MT5+MT6 D1V PIPELINE — START')
    logger.info('=' * 80)
    logger.info(f'Pipeline log: {log_path}')
    logger.info(f'Lookback: {config["days"]} days | '
                f'debug_writes: {config["debug_writes"]} | '
                f'progress_only: {config["progress_only"]}')

    try:
        ncd_mod = load_ncd_module()
        ncd_mod.main(config)
    except Exception as e:
        logger.error(f'Pipeline failed: {e}', exc_info=True)
        sys.exit(1)

    logger.info('=' * 80)
    logger.info('1278 NCD MT5+MT6 D1V PIPELINE — COMPLETE')
    logger.info('=' * 80)


if __name__ == '__main__':
    main()
