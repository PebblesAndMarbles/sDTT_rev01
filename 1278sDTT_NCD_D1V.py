# -*- coding: utf-8 -*-
"""
1278 NCD Measurement Pipeline — D1V, MT5+MT6
============================================
Standalone pipeline for NCD measurement data (A_8M5_FC_NCD + A_8M6_FC_NCD), mirroring
the query and join patterns of 1278sDTT_D1V_F32.py.

Purpose: pull NCD data at M5/M6 so that AME chamber (HM_ETCH) performance can be
observed at the NCD measurement step, even though AME does not own the NCD tool.
WEC context (HM_ETCH, SED, MAIN_ETCH) is resolved identically to the HCCD flow
to populate AME_ETCH, GTO_ETCH, SCANNER_MINIMAL, and RETICLE_MINIMAL columns.

Key differences from HCCD/DCCD/FCCD pipeline
---------------------------------------------
- NCD aliases: A_8M5_FC_NCD + A_8M6_FC_NCD
- MEASUREMENT_SET_NAME is UNKNOWN — a probe step runs at startup to discover it
  from live SPCS sessions before allstats/statistics/measurements queries run.
- Dual layer (MT5+MT6), single site (D1V), single CD type (NCD).
- No APC join, no checkpoint/resume, no BM0, no CD-level splitting.
- Output: integrated_output/1278sDTT_NCD_MT6_D1V.csv
    (filename retained for downstream compatibility; now contains MT5+MT6 rows)

Usage
-----
  python 1278sDTT_NCD_D1V.py          # standalone, uses CONFIG defaults
  python 1278sDTT_NCD_PIPELINE.py     # via orchestrator (preferred)
"""

import gc
import os
import time
import warnings
import logging
import re
import sys
from datetime import date, datetime

import pandas as pd
import numpy as np
import PyUber

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    'ceid': 'AMEct',
    'sites': ['D1V'],
    'database_connections': {
        'D1V': 'D1D_PROD_XEUS_LOCAL',
    },
    'tech': '1278',
    'tech_alias_nums': {'1278': '8'},
    'days': 3,
    'nLots_chunk': 25,
    'debug_writes': False,
    'folder_path': (
        r'\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson'
        r'\sDTT\sDTT_rev01\debug\1278 QUERY FILES\\'
    ),
    'main_csv_path': (
        r'\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson'
        r'\sDTT\sDTT_rev01\integrated_output\\'
    ),
    'main_csv_base_name': '1278sDTT',
    'output_csv_name': '1278sDTT_NCD_MT6_D1V',  # now contains MT5+MT6 rows
    'suppress_sqlalchemy_warnings': True,
    'log_level': 'INFO',
}

# ── NCD / WEC alias constants for MT5+MT6 ────────────────────────────────────
# CD aliases (measurement op groups)
NCD_ALIASES = ['A_8M5_FC_NCD', 'A_8M6_FC_NCD']
FIXED_NCD_MEASUREMENT_SET_NAME = 'GTOBE.PARAMETERS_DTT.78.DER'
FIXED_NCD_MONITOR_SET_NAME = 'GTOBE.INLINE_CD.78.MON'

# WEC aliases — same three families used by HCCD flow, expanded for M5+M6:
#   HM_ETCH  → AME etch chamber (source of AME_ETCH)
#   SED      → litho scanner context (source of SCANNER_MINIMAL, RETICLE_MINIMAL)
#   MAIN_ETCH → GTO etch context (source of GTO_ETCH); uses layer-1 (M5) per FCCD convention
WEC_HM_ETCH_ALIASES = ['E_8M5_HM_ETCH', 'E_8M6_HM_ETCH']
WEC_SED_ALIASES     = ['L_8M5_SED', 'L_8M6_SED']


def derive_main_etch_alias_from_hm_etch(hm_etch_alias):
    """Derive MAIN_ETCH alias by shifting HM_ETCH metal number down by 1.

    Pattern encoded for future layer expansion:
      E_8M6_HM_ETCH  -> E_8V5_MAIN_ETCH
      E_8M14_HM_ETCH -> E_8V13_MAIN_ETCH
    """
    m = re.fullmatch(r'E_(\d+)M(\d+)_HM_ETCH', hm_etch_alias)
    if not m:
        raise ValueError(f'Unexpected HM_ETCH alias format: {hm_etch_alias}')
    tech_prefix = m.group(1)
    hm_layer = int(m.group(2))
    main_layer = hm_layer - 1
    if main_layer < 1:
        raise ValueError(
            f'Cannot derive MAIN_ETCH alias from {hm_etch_alias}: shifted layer {main_layer} is invalid'
        )
    return f'E_{tech_prefix}V{main_layer}_MAIN_ETCH'


WEC_MAIN_ETCH_ALIASES = [derive_main_etch_alias_from_hm_etch(a) for a in WEC_HM_ETCH_ALIASES]

# Optional explicit pin to catch accidental pattern drift during refactors.
EXPECTED_WEC_MAIN_ETCH_ALIASES = {'E_8M5_HM_ETCH': 'E_8V4_MAIN_ETCH', 'E_8M6_HM_ETCH': 'E_8V5_MAIN_ETCH'}
for _hm_alias, _expected_main in EXPECTED_WEC_MAIN_ETCH_ALIASES.items():
    _derived_main = derive_main_etch_alias_from_hm_etch(_hm_alias)
    if _derived_main != _expected_main:
        raise ValueError(
            'Derived MAIN_ETCH alias does not match expected configured alias: '
            f'{_derived_main} != {_expected_main} (HM alias: {_hm_alias})'
        )

ALL_WEC_ALIASES = WEC_HM_ETCH_ALIASES + WEC_SED_ALIASES + WEC_MAIN_ETCH_ALIASES

# NCD alias -> corresponding HM_ETCH alias mapping for WEC join key assignment.
NCD_TO_WEC_HM_ALIAS_MAP = {
    'A_8M5_FC_NCD': 'E_8M5_HM_ETCH',
    'A_8M6_FC_NCD': 'E_8M6_HM_ETCH',
}


# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════

def setup_logging(log_level='INFO'):
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return logging.getLogger(__name__)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = os.path.join(log_dir, f'sdtt_ncd_{timestamp}.log')

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

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            SafeConsoleHandler(sys.stdout),
        ],
    )
    for _noisy in ('sqlalchemy.engine', 'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f'Logging initialized. Log file: {log_filename}')
    return logger


def setup_warning_filters(suppress=True):
    if suppress:
        warnings.filterwarnings(
            'ignore',
            message='.*pandas only supports SQLAlchemy connectable.*',
            category=UserWarning,
        )


# ══════════════════════════════════════════════════════════════════════════════
# DB retry helper
# ══════════════════════════════════════════════════════════════════════════════

def _read_sql_retry(sql, database_connection, max_retries=3, backoff_base=30):
    """Execute SQL via PyUber with retry on transient errors."""
    _logger = logging.getLogger(__name__)
    last_exc = None
    for attempt in range(1, max_retries + 1):
        _conn = None
        try:
            _conn = PyUber.connect(database_connection)
            return pd.read_sql(sql, _conn)
        except Exception as exc:
            last_exc = exc
            _logger.warning(
                f'DB query attempt {attempt}/{max_retries} failed: '
                f'{type(exc).__name__}: {str(exc)[:300]}'
            )
            if attempt < max_retries:
                wait = backoff_base * attempt
                _logger.info(f'Waiting {wait}s before reconnect/retry...')
                time.sleep(wait)
        finally:
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:
                    pass
                del _conn
                gc.collect()
    raise last_exc


# ══════════════════════════════════════════════════════════════════════════════
# Processor helper (file I/O)
# ══════════════════════════════════════════════════════════════════════════════

class SDTTProcessor:
    def __init__(self, config, site):
        self.config = config
        self.site = site
        self.database_connection = config['database_connections'][site]
        self.logger = logging.getLogger(f'sDTT_NCD_{site}')
        os.makedirs(config['folder_path'], exist_ok=True)
        os.makedirs(config['main_csv_path'], exist_ok=True)
        self.folder_path = config['folder_path']
        self.main_csv_path = config['main_csv_path']

    def df_to_csv(self, df, name):
        """Write intermediate debug CSV (only when debug_writes is True)."""
        if not self.config.get('debug_writes', False):
            return
        path = os.path.join(self.folder_path, f'{name}.csv')
        df.to_csv(path, index=False)
        self.logger.debug(f'Debug CSV written: {path}')

    def main_df_to_csv(self, df, name, no_index=None):
        """Atomic write to main output directory."""
        csvwritefile = os.path.join(self.main_csv_path, f'{name}.csv')
        tmpfile = csvwritefile + '.tmp'
        try:
            if no_index:
                df.to_csv(tmpfile, index=False)
            else:
                df.to_csv(tmpfile)
            os.replace(tmpfile, csvwritefile)
        except KeyboardInterrupt:
            if os.path.exists(tmpfile):
                try:
                    os.remove(tmpfile)
                except Exception:
                    pass
            self.logger.error(f'CSV write interrupted for {csvwritefile}')
            raise
        self.logger.debug(f'Output CSV written: {csvwritefile} ({len(df)} rows)')

    def main_csv_to_df(self, name):
        """Load existing output CSV or return empty DataFrame."""
        path = os.path.join(self.main_csv_path, f'{name}.csv')
        if os.path.exists(path):
            try:
                return pd.read_csv(path, low_memory=False)
            except Exception as e:
                self.logger.warning(f'Could not read existing CSV {path}: {e}')
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# Query builder
# ══════════════════════════════════════════════════════════════════════════════

class QueryBuilder:

    @staticmethod
    def operalias_query(ncd_alias_str):
        """Resolve NCD group alias → concrete operation numbers."""
        return f"""
        SELECT 'D1V' "SITE"
          ,a1.DATA_SOURCE
          ,a1.OPERATION
          ,a1.OPER_GROUP_KEY  "GROUP_KEY"
          ,a1.OPER_GROUP_NAME "ALIAS"
          ,a1.OPER_INTEGRATION_LAYER "LAYER"
          ,a1.OPER_SHORT_DESC
          ,a1.OPER_LONG_DESC
        FROM F_OPERATION_ALIAS a1
        WHERE a1.DATA_SOURCE IN ('D1_P1278')
          AND UPPER(a1.OPER_GROUP_NAME) IN ({ncd_alias_str})
        """

    @staticmethod
    def process_op_aliases_query(wec_aliases_str):
        """Resolve WEC group aliases → concrete operation numbers."""
        return f"""
        SELECT 'D1V' "SITE"
          ,a1.DATA_SOURCE
          ,a1.OPERATION
          ,a1.OPER_GROUP_KEY  "GROUP_KEY"
          ,a1.OPER_GROUP_NAME "ALIAS"
          ,a1.OPER_INTEGRATION_LAYER "LAYER"
          ,a1.OPER_SHORT_DESC
          ,a1.OPER_LONG_DESC
        FROM F_OPERATION_ALIAS a1
        WHERE a1.DATA_SOURCE IN ('D1_P1278')
          AND UPPER(a1.OPER_GROUP_NAME) IN ({wec_aliases_str})
        """

    @staticmethod
    def spclot_prefetch_query(days, mops_str):
        return f"""
        SELECT 'D1V' "SITE"
          ,l.DATA_COLLECTION_TIME
          ,l.LOT "LOT"
          ,l.LOT7 "LOT7"
          ,l.LOT_TYPE "LOT_TYPE"
          ,l.OPERATION
          ,l.ROUTE
          ,l.PRODUCT "PRODUCT"
          ,l.LOT_PROCESS
          ,l.DEVREVSTEP "PRODUCT DEVREVSTEP"
          ,l.MONITOR_TYPE
          ,l.MONITOR_SET_NAME
          ,l.TEST_NAME
          ,l.PILOT_NAME
        FROM P_SPC_LOT l
        WHERE l.DATA_COLLECTION_TIME >= TRUNC(CURRENT_DATE) - {days}
          AND l.OPERATION IN ({mops_str})
          AND l.LOT_PROCESS = '1278'
                    AND l.MONITOR_SET_NAME = '{FIXED_NCD_MONITOR_SET_NAME}'
        """

    @staticmethod
    def lot_run_card_query(spc_lot_str, mops_str):
        return f"""
        SELECT l.LOT "LOT"
          ,l.OPERATION
          ,l.DATA_COLLECTION_TIME
          ,TO_CHAR(CAST(l.SPCS_ID AS DECIMAL(20,10))) "SPCS_ID"
          ,l.SPC_DATA_ID
        FROM F_LOT_RUN_CARD h
        INNER JOIN P_SPC_LOT l
          ON  l.LOTOPERKEY = h.LOTOPERKEY
          AND l.MONITOR_TYPE = 'WIP MONITOR'
                    AND l.MONITOR_SET_NAME = '{FIXED_NCD_MONITOR_SET_NAME}'
        INNER JOIN P_SPC_SESSION s
          ON  s.SPCS_ID = l.SPCS_ID
          AND s.LATEST_FLAG = 'Y'
        WHERE h.LOT IN ({spc_lot_str})
          AND h.OPERATION IN ({mops_str})
        """

    @staticmethod
    def probe_measurement_sets_query(spcs_id_str):
        """Discover MEASUREMENT_SET_NAME and sample ATTRIBUTES for NCD sessions.

        This query is intentionally broad — no measurement-set or structure
        filter — so we can see exactly what the NCD sessions contain before
        deciding on the allstats/statistics/measurements filters.
        """
        return f"""
        SELECT DISTINCT
            m.MEASUREMENT_SET_NAME,
            a.ATTRIBUTES
        FROM P_SPC_SESSION s
        INNER JOIN P_SPC_MEASUREMENT m
          ON  m.SPCS_ID = s.SPCS_ID
          AND m.STATUS NOT IN ('M', 'I')
        INNER JOIN P_SPC_MEASUREMENT_ATTRIBUTE a
          ON  a.ATTRIBUTE_ID = m.ATTRIBUTE_ID
        WHERE s.SPCS_ID IN ({spcs_id_str})
        """

    @staticmethod
    def allstats_query(spcs_id_str, mset_filter_sql):
        """allstats query — fixed to NCD DTT measurement set."""
        return f"""
        SELECT m.DATA_COLLECTION_TIME
          ,m.LOT "LOT"
          ,m.WAFER "WAFER_ID"
          ,m.OPERATION "OPERATION"
          ,TO_CHAR(CAST(m.SPCS_ID AS DECIMAL(20,10))) "SPCS_ID"
          ,s.TEST_NAME
          ,m.MEASUREMENT_SET_NAME
          ,m.PRIMARY_ENTITY
          ,m.VALID_FLAG
          ,m.STANDARD_FLAG
          ,m.CORRECTED_FLAG
          ,m.MEASUREMENT_ID
          ,a.ATTRIBUTES
          ,a.PARAMETERS
          ,m.VALUE
          ,m.STATUS
          ,s.ANALYTICAL_ENTITY
        FROM P_SPC_SESSION s
        INNER JOIN P_SPC_MEASUREMENT m
          ON  m.SPCS_ID = s.SPCS_ID
          AND m.STATUS NOT IN ('M', 'I')
        INNER JOIN P_SPC_MEASUREMENT_ATTRIBUTE a
          ON  a.ATTRIBUTE_ID = m.ATTRIBUTE_ID
        WHERE s.SPCS_ID IN ({spcs_id_str})
                    AND m.MEASUREMENT_SET_NAME = '{FIXED_NCD_MEASUREMENT_SET_NAME}'
        """

    @staticmethod
    def spc_measurements_query(spcs_id_str, mset_filter_sql):
        """Point-level measurements — fixed to NCD DTT measurement set."""
        return f"""
        SELECT m.LOT "LOT"
          ,m.OPERATION "OPERATION"
          ,TO_CHAR(CAST(m.SPCS_ID AS DECIMAL(20,10))) "SPCS_ID"
          ,s.TEST_NAME
          ,m.MEASUREMENT_SET_NAME
          ,m.PRIMARY_ENTITY
          ,m.WAFER "WAFER_ID"
          ,m.VALID_FLAG
          ,m.STANDARD_FLAG
          ,m.CORRECTED_FLAG
          ,m.WAFER_COORDINATE_X "WAFER_X"
          ,m.WAFER_COORDINATE_Y "WAFER_Y"
          ,m.NATIVE_X
          ,m.NATIVE_Y
          ,m.NATIVE_X_COL
          ,m.NATIVE_Y_ROW
          ,m.X_DIE
          ,m.Y_DIE
          ,m.FIELD_X_COL
          ,m.FIELD_Y_ROW
          ,m.WITHIN_FIELD_X
          ,m.WITHIN_FIELD_Y
          ,m.MEASUREMENT_ID
          ,a.ATTRIBUTES
          ,a.PARAMETERS
          ,m.VALUE
          ,m.STATUS
          ,m.DATA_COLLECTION_TIME
          ,m.LOT "ACTUAL_LOT"
          ,s.ANALYTICAL_ENTITY
          ,r.RECIPE "WAFER_RECIPE"
        FROM P_SPC_SESSION s
        INNER JOIN P_SPC_MEASUREMENT m
          ON  m.SPCS_ID = s.SPCS_ID
          AND m.STATUS NOT IN ('M', 'I')
        INNER JOIN P_SPC_MEASUREMENT_ATTRIBUTE a
          ON  a.ATTRIBUTE_ID = m.ATTRIBUTE_ID
        LEFT JOIN F_WAFERENTITYHIST w
          ON  w.BATCH_ID = s.BATCH_ID
          AND w.WAFER = m.WAFER
        LEFT JOIN F_LOT_WAFER_RECIPE r
          ON  r.RECIPE_ID = w.WAFER_RECIPE_ID
        WHERE s.SPCS_ID IN ({spcs_id_str})
                    AND m.MEASUREMENT_SET_NAME = '{FIXED_NCD_MEASUREMENT_SET_NAME}'
        """

    @staticmethod
    def statistics_query(spcs_id_str, mset_filter_sql):
        """SPC chart point statistics — fixed to NCD DTT measurement set."""
        return f"""
        SELECT x.*
        FROM (
          SELECT cp.DATA_COLLECTION_TIME
            ,l.LOT "LOT"
            ,l.OPERATION "OPERATION"
            ,TO_CHAR(CAST(cp.SPCS_ID AS DECIMAL(20,10))) "SPCS_ID"
            ,cp.MEASUREMENT_SET_NAME
            ,cp.TEST_NAME
            ,cp.CHART_TYPE
            ,c.CHART_ATTRIBUTES
            ,cp.VALID_FLAG
            ,cp.STANDARD_FLAG
            ,cp.CORRECTED_FLAG
            ,cp.INCONTROL_FLAG
            ,cp.INDISPOSITION_FLAG
            ,cp.VIOLATED_RULE_NOTATION
            ,cp.CHART_ID
            ,cp.CHART_POINT_SEQ
            ,cp.VALUE
            ,NVL(cp.WAFER, w.WAFER) "WAFER_ID"
            ,cl.CENTERLINE
            ,cl.TARGET
            ,cl.LO_CONTROL_LMT "LCL"
            ,cl.UP_CONTROL_LMT "UCL"
            ,cl.LO_DISPOSITION_LMT "LDL"
            ,cl.UP_DISPOSITION_LMT "UDL"
            ,cl.LO_SPEC_LMT "LSL"
            ,cl.UP_SPEC_LMT "USL"
            ,DENSE_RANK() OVER (
                PARTITION BY l.LOT, l.OPERATION, cp.MEASUREMENT_SET_NAME, cp.CHART_TYPE
                ORDER BY cp.DATA_COLLECTION_TIME DESC
            ) "PASS_ORDER"
          FROM P_SPC_CHART_POINT cp
          INNER JOIN P_SPC_LOT l
            ON  l.SPCS_ID = cp.SPCS_ID
          INNER JOIN P_SPC_CHART c
            ON  c.CHART_ID = cp.CHART_ID
          LEFT JOIN P_SPC_CHART_LIMIT cl
            ON  cp.CHART_ID = cl.CHART_ID
            AND cp.LIMIT_ID = cl.LIMIT_ID
          LEFT JOIN P_SPC_CHARTPOINT_WAFER w
            ON  w.SPCS_ID = cp.SPCS_ID
            AND w.CHART_ID = cp.CHART_ID
            AND w.CHART_POINT_SEQ = cp.CHART_POINT_SEQ
          WHERE cp.SPCS_ID IN ({spcs_id_str})
                        AND cp.MEASUREMENT_SET_NAME = '{FIXED_NCD_MEASUREMENT_SET_NAME}'
            AND NVL(cp.WAFER, w.WAFER) IS NOT NULL
        ) x
        WHERE x.PASS_ORDER = 1
        """

    @staticmethod
    def wec_query_optimized(spc_lot_str, wec_op_str, wafer_chunk_str):
        site = 'D1V'
        return f"""
        SELECT c.LOT "LOT"
          ,c.WAFER "WAFER_ID"
          ,c.WAF3 "WAFER_SHORT"
          ,c.OPERATION
          ,c.ROUTE
          ,c.ENTITY
          ,TO_CHAR(w.BATCH_ID) "DB_BATCH_ID"
          ,c.CHAMBER
          ,c.SUBENTITY
          ,c.SUB_OPERATION
          ,CAST(c.END_TIME AS DATE) "SUBENTITY_END_TIME"
          ,CAST(c.START_TIME AS DATE) "SUBENTITY_START_TIME"
          ,CAST(w.WAFER_ENTITY_END_TIME AS DATE) "ENTITY_END_TIME"
          ,CAST(w.WAFER_ENTITY_START_TIME AS DATE) "ENTITY_START_TIME"
          ,SUBSTR(c.ENTITY, 1, 3) "ENTITY_PREFIX"
          ,TO_CHAR(CAST(h.RUNKEY AS DECIMAL(20,10))) "RUNKEY"
          ,c.CHAMBER_PROCESS_DURATION "PROCESS_TIME"
          ,c.CHAMBER_PROCESS_ORDER "PROCESS_ORDER"
          ,c.CHAMBER_WAIT_DURATION "WAIT_TIME"
          ,CAST(h.LAST_TXN_TIME AS DATE) "LAST_TXN_TIME"
          ,c.SLOT
          ,NVL(h.ENTITY_OWNED_BY, '{site}') "ENTITY_OWNED_BY"
          ,r.RECIPE
        FROM F_LOT_RUN_MAP h
        INNER JOIN F_WAFERENTITYHIST w
          ON  w.RUNKEY = h.RUNKEY
          AND w.EXPECTED_LOT = h.EXPECTED_LOT
          AND w.WAFER IS NOT NULL
          AND w.IS_CONDITIONING_WAFER IS NULL
        INNER JOIN F_WAFERCHAMBERHIST c
          ON  c.RUNKEY = w.RUNKEY
          AND c.WAFER = w.WAFER
          AND c.ENTITY = w.ENTITY
        INNER JOIN F_LOT_WAFER_RECIPE r
          ON  r.RECIPE_ID = w.WAFER_RECIPE_ID
        WHERE h.EXPECTED_LOT IN ({spc_lot_str})
          AND h.OPERATION IN ({wec_op_str})
          AND c.WAFER IN ({wafer_chunk_str})
        """

    @staticmethod
    def wec_query_sed_only(sed_op_str, wafer_chunk_str):
        return f"""
        SELECT DISTINCT c.WAFER "WAFER_ID"
          ,c.ENTITY "SCANNER"
          ,c.SUBENTITY
          ,c.OPERATION
          ,w.RETICLE
        FROM F_WAFERCHAMBERHIST c
        LEFT JOIN F_WAFERENTITYHIST w ON w.WAFER = c.WAFER
        WHERE c.OPERATION IN ({sed_op_str})
          AND c.WAFER IN ({wafer_chunk_str})
        """

    @staticmethod
    def wec_query_etch_only(etch_op_str, wafer_chunk_str):
        return f"""
        SELECT DISTINCT c.WAFER "WAFER_ID"
          ,c.ENTITY "SCANNER"
          ,c.SUBENTITY
          ,c.OPERATION
        FROM F_WAFERCHAMBERHIST c
        WHERE c.OPERATION IN ({etch_op_str})
          AND c.WAFER IN ({wafer_chunk_str})
        """


# ══════════════════════════════════════════════════════════════════════════════
# Data processor
# ══════════════════════════════════════════════════════════════════════════════

class DataProcessor:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def parse_attributes(attr_string):
        attributes = {}
        for pair in attr_string.strip(';').split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                attributes[k] = v
        return attributes

    @staticmethod
    def chunk_list(items, chunk_length):
        return [items[i:i + chunk_length] for i in range(0, len(items), chunk_length)]

    def process_measurements_data(self, df_raw, processor):
        self.logger.info(f'Processing measurements: {len(df_raw)} raw records')
        parsed = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        df_attr = pd.concat([df_raw, pd.DataFrame(parsed.tolist())], axis=1).drop('ATTRIBUTES', axis=1)

        # Fixed DTT monitor rows can include non-numeric coordinates; coerce to NaN
        # so radius math remains vectorized and robust.
        df_attr['WAFER_X'] = pd.to_numeric(df_attr['WAFER_X'], errors='coerce')
        df_attr['WAFER_Y'] = pd.to_numeric(df_attr['WAFER_Y'], errors='coerce')
        df_attr['WAFER_RADIUS'] = np.sqrt(df_attr['WAFER_X'] ** 2 + df_attr['WAFER_Y'] ** 2)
        df_attr['WAFER_RECIPE'] = df_attr['WAFER_RECIPE'].fillna('MISSING')

        # MEASURE_INDEX is present in HCCD/DCCD/FCCD attributes but may be absent
        # for NCD sessions.  If missing, assign a constant '1' so the pivot still
        # produces VALUE_1 / WAFER_RADIUS_1 column names (single-point per wafer).
        if 'MEASURE_INDEX' not in df_attr.columns:
            self.logger.warning(
                'MEASURE_INDEX not found in parsed ATTRIBUTES — NCD measurements '
                'may be single-point per session.  Assigning MEASURE_INDEX=1.'
            )
            df_attr['MEASURE_INDEX'] = '1'

        df_pivot = df_attr.pivot_table(
            index=['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE'],
            columns='MEASURE_INDEX',
            values=['WAFER_RADIUS', 'VALUE'],
            aggfunc='first',
        )
        df_pivot.columns = ['_'.join(str(c) for c in col).strip() for col in df_pivot.columns]
        df_pivot.reset_index(inplace=True)

        # CD and LAYER from TEST_NAME — same slice positions as existing pipeline;
        # actual values will depend on NCD TEST_NAME format discovered in probe.
        df_pivot['CD'] = df_pivot['TEST_NAME'].str.slice(4, 8)
        cond = df_pivot['TEST_NAME'].str.endswith('H')
        df_pivot.loc[cond, 'CD'] = 'H' + df_pivot.loc[cond, 'CD'].str[1:]
        df_pivot['LAYER'] = df_pivot['TEST_NAME'].str.slice(9, 12)

        self.logger.info(f'Measurements pivoted: {len(df_pivot)} records')
        return df_pivot

    def process_allstats_data(self, df_raw, processor):
        self.logger.info(f'Processing allstats: {len(df_raw)} raw records')
        parsed = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        df_attr = pd.concat([df_raw, pd.DataFrame(parsed.tolist())], axis=1)
        df_attr = df_attr.drop(['ATTRIBUTES', 'PARAMETERS', 'MEASUREMENT_ID'], axis=1)
        df_attr['PILOT_NAME'] = df_attr.get('PILOT_NAME', pd.Series('MISSING', index=df_attr.index)).fillna('MISSING')

        # DYNWAFER and IS_POR expected in parsed attributes; guard if absent
        pivot_index = ['WAFER_ID', 'SPCS_ID']
        for optional in ['DYNWAFER', 'IS_POR']:
            if optional in df_attr.columns:
                pivot_index.append(optional)
            else:
                self.logger.warning(
                    f'allstats ATTRIBUTES did not contain {optional} — column absent from pivot index. '
                    f'NCD measurement set may use different attribute keys.'
                )

        # Detect the pivot column key:
        #   NCD GTOBE real data uses 'STATISTICS' (values: MEAN_DTT, SIGMA_DTT)
        #   FCCD/HCCD/PLI CU data uses 'CD_TERMS'
        #   NCD DUMMY/PLI sessions use 'PARAMETER_NAME'
        if 'STATISTICS' in df_attr.columns:
            pivot_key = 'STATISTICS'
            self.logger.info('Using STATISTICS as pivot column key (NCD GTOBE data — MEAN_DTT/SIGMA_DTT).')
        elif 'CD_TERMS' in df_attr.columns:
            pivot_key = 'CD_TERMS'
        elif 'PARAMETER_NAME' in df_attr.columns:
            pivot_key = 'PARAMETER_NAME'
            self.logger.info(
                'CD_TERMS absent — using PARAMETER_NAME as pivot column key '
                '(typical for NCD DUMMY/PLI sessions).'
            )
        else:
            self.logger.error(
                'Neither STATISTICS, CD_TERMS nor PARAMETER_NAME found in parsed ATTRIBUTES. '
                'Assigning CD_TERMS=UNKNOWN as placeholder — check probe ATTRIBUTES output.'
            )
            df_attr['CD_TERMS'] = 'UNKNOWN'
            pivot_key = 'CD_TERMS'

        pivot_column = [pivot_key]
        pivot_values = ['VALUE']

        df_pivot_nomerge = df_attr.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first'
        )
        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(str(c) for c in col).strip() for col in df_pivot_nomerge.columns]
        df_pivot_nomerge.reset_index(inplace=True)

        df_merge_cols = df_attr.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='inner')
        self.logger.info(f'Allstats pivoted: {len(df_pivot)} records (pivot key: {pivot_key})')
        return df_pivot

    def process_statistics_data(self, df_raw, processor):
        self.logger.info(f'Processing statistics: {len(df_raw)} raw records')
        if df_raw.empty:
            self.logger.info('Statistics: empty DataFrame — skipping pivot, returning empty')
            return pd.DataFrame()
        if 'CHART_ATTRIBUTES' not in df_raw.columns:
            self.logger.warning('CHART_ATTRIBUTES column missing from statistics — returning empty')
            return pd.DataFrame()
        parsed = df_raw['CHART_ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        df_attr = pd.concat([df_raw, pd.DataFrame(parsed.tolist())], axis=1).drop('CHART_ATTRIBUTES', axis=1)

        pivot_index = ['WAFER_ID', 'SPCS_ID']
        if 'STATISTICS' in df_attr.columns:
            stats_pivot_key = 'STATISTICS'
            self.logger.info('Statistics: using STATISTICS as pivot key (NCD GTOBE data).')
        elif 'CD_TERMS' in df_attr.columns:
            stats_pivot_key = 'CD_TERMS'
        elif 'PARAMETER_NAME' in df_attr.columns:
            stats_pivot_key = 'PARAMETER_NAME'
            self.logger.info('Statistics: using PARAMETER_NAME as pivot key')
        else:
            self.logger.warning('Statistics: no STATISTICS, CD_TERMS, or PARAMETER_NAME — returning empty')
            return pd.DataFrame()
        pivot_column = [stats_pivot_key]
        pivot_values = [
            'VALUE', 'CENTERLINE', 'TARGET', 'LCL', 'UCL', 'LDL', 'UDL', 'LSL', 'USL',
            'VALID_FLAG', 'STANDARD_FLAG', 'CORRECTED_FLAG', 'INCONTROL_FLAG',
            'INDISPOSITION_FLAG', 'VIOLATED_RULE_NOTATION', 'CHART_ID', 'CHART_TYPE',
            'CHART_POINT_SEQ',
        ]

        df_pivot_nomerge = df_attr.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first'
        )
        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(str(c) for c in col).strip() for col in df_pivot_nomerge.columns]
        df_pivot_nomerge.reset_index(inplace=True)

        df_merge_cols = df_attr.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='outer')
        self.logger.info(f'Statistics pivoted: {len(df_pivot)} records')
        return df_pivot


# ══════════════════════════════════════════════════════════════════════════════
# WEC helpers  (identical to 1278sDTT_D1V_F32.py)
# ══════════════════════════════════════════════════════════════════════════════

def process_minimal_wec_data_separate(df_wec_sed, df_wec_etch, df_minimal_op_aliases):
    logger = logging.getLogger(__name__)

    # SED → SCANNER_MINIMAL + RETICLE_MINIMAL
    sed_proc = df_wec_sed.groupby('WAFER_ID').agg(
        SCANNER_MINIMAL=('SCANNER', 'first'),
        RETICLE_MINIMAL=('RETICLE', 'first'),
    ).reset_index()

    # ETCH → AME_ETCH (HM_ETCH) + GTO_ETCH (MAIN_ETCH)
    if not df_wec_etch.empty:
        df_ea = pd.merge(df_wec_etch, df_minimal_op_aliases[['OPERATION', 'ALIAS']],
                         on='OPERATION', how='left')
        hm   = df_ea[df_ea['ALIAS'].str.contains('HM_ETCH',   na=False)]
        main = df_ea[df_ea['ALIAS'].str.contains('MAIN_ETCH', na=False)]

        ame = hm.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        ame.columns = ['WAFER_ID', 'AME_ETCH']

        gto = main.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        gto.columns = ['WAFER_ID', 'GTO_ETCH']

        etch_proc = pd.merge(ame, gto, on='WAFER_ID', how='outer')
    else:
        etch_proc = pd.DataFrame(columns=['WAFER_ID', 'AME_ETCH', 'GTO_ETCH'])

    result = pd.merge(sed_proc, etch_proc, on='WAFER_ID', how='outer')
    logger.info(
        f'WEC minimal processed: {len(result)} wafers | '
        f'AME_ETCH {result.get("AME_ETCH", pd.Series()).notna().sum()} | '
        f'GTO_ETCH {result.get("GTO_ETCH", pd.Series()).notna().sum()}'
    )
    return result


def join_chunk_data(processor, df_allstats, df_statistics,
                    df_wec, df_operalias, ncd_to_wec_alias_map):
    logger = logging.getLogger(__name__)
    # Include MEASUREMENT_SET_NAME to prevent cross-set many-to-many joins.
    common = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID', 'MEASUREMENT_SET_NAME']

    allstats_df = df_allstats.rename(
        columns={c: 'ALLSTATS_' + c for c in df_allstats.columns if c not in common})
    if not df_statistics.empty:
        statistics_df = df_statistics.rename(
            columns={c: 'STATISTICS_' + c for c in df_statistics.columns if c not in common})
        df_sj = pd.merge(allstats_df, statistics_df, on=common, how='outer')
    else:
        logger.info('Statistics DataFrame empty — skipping statistics merge')
        df_sj = allstats_df
    processor.df_to_csv(df_sj, f'spc_join_D1V')
    logger.debug(f'allstats+statistics: {len(df_sj)}')

    # Raw measurements query is intentionally disabled for NCD flow; keep
    # join path allstats+statistics only to avoid extra many-to-many surfaces.
    df_sj2 = df_sj

    # Join with NCD operation alias
    alias2op = df_operalias[['OPERATION', 'ALIAS']].rename(
        columns={'OPERATION': 'ALLSTATS_OPERATION', 'ALIAS': 'ALLSTATS_ALIAS'})
    df_sj3 = pd.merge(df_sj2, alias2op, on='ALLSTATS_OPERATION', how='inner')
    df_sj3.rename(columns={'ALLSTATS_ALIAS': 'SPC_ALIAS'}, inplace=True)

    # Map NCD alias -> layer-matched HM_ETCH alias (MT5 and MT6 supported).
    df_sj3['WEC_ALIAS'] = df_sj3['SPC_ALIAS'].map(ncd_to_wec_alias_map)
    unresolved = int(df_sj3['WEC_ALIAS'].isna().sum())
    if unresolved:
        logger.warning(
            f'WEC_ALIAS mapping unresolved for {unresolved} rows. '
            'Check NCD_TO_WEC_HM_ALIAS_MAP for new aliases.'
        )
    processor.df_to_csv(df_sj3, 'spc_join3_D1V')
    logger.debug(f'+operalias: {len(df_sj3)}')

    # Final join with WEC
    wec_merge = df_wec.rename(
        columns={c: 'WEC_' + c for c in df_wec.columns if c not in ['WEC_ALIAS', 'WAFER_ID']})
    df_sdtt = pd.merge(df_sj3, wec_merge, on=['WEC_ALIAS', 'WAFER_ID'], how='inner')
    processor.df_to_csv(df_sdtt, 'sdtt_chunk_D1V')
    logger.debug(f'+WEC final: {len(df_sdtt)}')
    return df_sdtt


# ══════════════════════════════════════════════════════════════════════════════
# Post-processing transforms  (identical to 1278sDTT_D1V_F32.py)
# ══════════════════════════════════════════════════════════════════════════════

def add_esc_zones(SDTT):
    logger = logging.getLogger(__name__)
    added = 0
    for col in SDTT.columns:
        if 'WAFER_RADIUS' in col:
            new_col = f'{col}_ZONE'
            conds = [
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col] < 38),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col] >= 38) & (SDTT[col] < 108),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col] >= 108) & (SDTT[col] < 128.5),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col] >= 128.5),
            ]
            SDTT[new_col] = np.select(conds, ['I', 'MI', 'MO', 'O'], default='')
            added += 1
    logger.debug(f'ESC zone columns added: {added}')


def add_derived_columns(SDTT):
    logger = logging.getLogger(__name__)
    op_col, prod_col, pilot_col = 'ALLSTATS_OPERATION', 'PRODUCT', 'ALLSTATS_PILOT_NAME'
    if op_col in SDTT.columns and prod_col in SDTT.columns:
        SDTT['PROD_MOP'] = SDTT[prod_col].astype(str) + '_' + SDTT[op_col].astype(str)
    if all(c in SDTT.columns for c in [op_col, prod_col, pilot_col]):
        SDTT['PROD_MOP_PILOT'] = (
            SDTT[prod_col].astype(str) + '_' + SDTT[op_col].astype(str)
            + '_' + SDTT[pilot_col].astype(str)
        )


def rename_final_columns(SDTT):
    renames = {
        'ALLSTATS_PRODUCT_GROUP': 'PRODUCT_GROUP',
        'PRODUCT DEVREVSTEP':     'PRODUCT',
        'ALLSTATS_STRUCTURE':     'STRUCTURE',
        'ALLSTATS_ROUTE_TYPE':    'ROUTE_TYPE',
        'ALLSTATS_IS_POR':        'IS_POR',
        'MEASUREMENTS_WAFER_RECIPE': 'SPC_RECIPE',
        'WEC_WAFER_SHORT':        'WID',
        'WEC_DB_BATCH_ID':        'DB_BATCH_ID',
        'ALLSTATS_PRIMARY_ENTITY': 'PRIMARY_ENTITY',
        'ALLSTATS_ANALYTICAL_ENTITY': 'ANALYTICAL_ENTITY',
        'WEC_SUBENTITY':          'SUBENTITY',
        'MEASUREMENTS_CD':        'CD',
        'MEASUREMENTS_LAYER':     'LAYER',
        'WEC_RETICLE_MINIMAL':    'RETICLE',
        'WEC_SCANNER_MINIMAL':    'SCANNER',
        'WEC_AME_ETCH':           'AME_ETCH',
        'WEC_GTO_ETCH':           'GTO_ETCH',
    }
    for old, new in renames.items():
        if old in SDTT.columns:
            SDTT.rename(columns={old: new}, inplace=True)
    if 'WEC_SUBENTITY_END_TIME' in SDTT.columns:
        SDTT['SUBENTITY_END_TIME'] = SDTT['WEC_SUBENTITY_END_TIME'].copy()
    if 'SPC_ROUTE' in SDTT.columns:
        SDTT['ROUTE'] = SDTT['SPC_ROUTE'].copy()


def reorder_columns(SDTT):
    priority = [
        'DATA_COLLECTION_TIME',
        'SPC_LOT', 'WID', 'IS_POR', 'WAFER_ID', 'TEST_NAME', 'PRODUCT',
        'SUBENTITY', 'SUBENTITY_END_TIME',
        'WEC_OPERATION', 'WEC_RECIPE', 'WEC_LAYER',
        'ALLSTATS_MEAN_DTT_VALUE', 'ALLSTATS_MEAN_TARGET_VALUE', 'ALLSTATS_WAFER_MEAN_VALUE',
        'ALLSTATS_SIGMA_DTT_VALUE', 'ALLSTATS_SIGMA_TARGET_VALUE', 'ALLSTATS_WAFER_SIGMA_VALUE',
        'ROUTE', 'SPC_OPERATION', 'PROD_MOP', 'PROD_MOP_PILOT',
        'ANALYTICAL_ENTITY', 'PRIMARY_ENTITY',
        'SPC_RECIPE', 'SPC_PILOT_NAME', 'SPC_ALIAS',
        'SCANNER', 'RETICLE', 'AME_ETCH', 'GTO_ETCH',
        'WEC_LOT', 'WEC_ALIAS', 'ROUTE_TYPE', 'LAYER', 'CD',
        'STRUCTURE', 'PRODUCT_GROUP', 'LOT7', 'LOT_TYPE', 'SPCS_ID', 'DB_BATCH_ID',
    ]
    current = SDTT.columns.tolist()
    remaining = sorted([c for c in current if c not in priority])
    existing_priority = [c for c in priority if c in current]
    return SDTT.reindex(columns=existing_priority + remaining)


def cleanup_and_sort(SDTT):
    unnamed = SDTT.columns[SDTT.columns.str.contains('Unnamed', case=False)]
    if len(unnamed):
        SDTT.drop(columns=unnamed, inplace=True)
    for col in ['SUBENTITY_END_TIME', 'DATA_COLLECTION_TIME']:
        if col in SDTT.columns:
            SDTT[col] = pd.to_datetime(SDTT[col], format='mixed', dayfirst=False, errors='coerce')
    SDTT.reset_index(drop=True, inplace=True)


# ══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ══════════════════════════════════════════════════════════════════════════════

def main(config=None):
    if config is None:
        config = CONFIG

    setup_warning_filters(config.get('suppress_sqlalchemy_warnings', True))
    logger = setup_logging(config.get('log_level', 'INFO'))

    logger.info('=' * 80)
    logger.info('1278 NCD MT5+MT6 D1V Pipeline — START')
    logger.info('=' * 80)
    logger.info(f'Config: days={config["days"]}, nLots_chunk={config["nLots_chunk"]}, '
                f'debug_writes={config["debug_writes"]}')

    site = 'D1V'
    db   = config['database_connections'][site]
    processor     = SDTTProcessor(config, site)
    data_processor = DataProcessor()

    # ── Step 1: Resolve NCD operation numbers ─────────────────────────────────
    logger.info('─' * 60)
    logger.info('STEP 1 — Resolve NCD aliases -> operation numbers')
    ncd_alias_str = ','.join(f"'{a}'" for a in NCD_ALIASES)
    df_operalias = _read_sql_retry(QueryBuilder.operalias_query(ncd_alias_str), db)
    logger.info(f'NCD aliases {NCD_ALIASES} -> {len(df_operalias)} operations: '
                f'{df_operalias["OPERATION"].tolist()}')
    processor.df_to_csv(df_operalias, 'ncd_oper_alias_D1V_M5_M6')

    if df_operalias.empty:
        logger.error(f'No operations found for aliases {NCD_ALIASES}. Aborting.')
        return
    mops_str = ','.join(f"'{op}'" for op in df_operalias['OPERATION'])

    # ── Step 2: Resolve WEC process operation aliases ─────────────────────────
    logger.info('─' * 60)
    logger.info('STEP 2 — Resolve WEC aliases → operation numbers')
    wec_aliases_str = ','.join(f"'{a}'" for a in ALL_WEC_ALIASES)
    df_wec_op_aliases = _read_sql_retry(QueryBuilder.process_op_aliases_query(wec_aliases_str), db)
    logger.info(f'WEC aliases resolved: {len(df_wec_op_aliases)} operations')
    processor.df_to_csv(df_wec_op_aliases, 'ncd_wec_op_aliases_D1V_M5_M6')
    wec_op_str = ','.join(f"'{op}'" for op in df_wec_op_aliases['OPERATION'])

    sed_ops   = df_wec_op_aliases[df_wec_op_aliases['ALIAS'].str.contains('SED',       na=False)]['OPERATION'].tolist()
    hm_ops    = df_wec_op_aliases[df_wec_op_aliases['ALIAS'].str.contains('HM_ETCH',   na=False)]['OPERATION'].tolist()
    main_ops  = df_wec_op_aliases[df_wec_op_aliases['ALIAS'].str.contains('MAIN_ETCH', na=False)]['OPERATION'].tolist()
    etch_ops  = hm_ops + main_ops

    sed_op_str  = ','.join(f"'{op}'" for op in sed_ops)  if sed_ops  else "''"
    etch_op_str = ','.join(f"'{op}'" for op in etch_ops) if etch_ops else "''"

    df_minimal_op_aliases = df_wec_op_aliases.copy()

    # ── Step 3: SPC lot prefetch ──────────────────────────────────────────────
    logger.info('─' * 60)
    logger.info('STEP 3 — SPC lot prefetch')
    df_spclot = _read_sql_retry(QueryBuilder.spclot_prefetch_query(config['days'], mops_str), db)
    lots = df_spclot['LOT'].drop_duplicates().tolist()
    logger.info(f'Lots found (last {config["days"]} days): {len(lots)}')
    logger.info(f'TEST_NAME values seen in prefetch: {df_spclot["TEST_NAME"].dropna().unique().tolist()}')
    processor.df_to_csv(df_spclot, 'ncd_spc_lot_prefetch_D1V_M6')

    if not lots:
        logger.warning(f'No lots found for NCD MT5/MT6 in the last {config["days"]} days. '
                   f'Try increasing days or verify aliases {NCD_ALIASES} are active.')
        return

    # ── Step 4: Lot run card → SPCS IDs ──────────────────────────────────────
    logger.info('─' * 60)
    logger.info('STEP 4 — Lot run card → SPCS IDs (sample for probe)')
    lot_chunks = DataProcessor.chunk_list(lots, config['nLots_chunk'])

    # Run one small batch just to get SPCS IDs for the probe
    probe_lots_str = ','.join(f"'{l}'" for l in lot_chunks[0])
    df_lrc_probe = _read_sql_retry(QueryBuilder.lot_run_card_query(probe_lots_str, mops_str), db)
    probe_spcs = df_lrc_probe['SPCS_ID'].drop_duplicates().tolist()

    # ── Step 5: PROBE — discover/validate measurement sets ─────────────────────
    logger.info('─' * 60)
    logger.info('STEP 5 — PROBE: Discover MEASUREMENT_SET_NAME and ATTRIBUTES for NCD')
    # Defaults — overwritten by probe if SPCS IDs are available
    mset_filter_sql       = '1=1'
    mset_filter_sql_stats = '1=1'
    discovered_msets      = []

    if not probe_spcs:
        logger.warning('Probe: no SPCS IDs in first lot batch — probe skipped. '
                       'Allstats/statistics queries will proceed unfiltered.')
    else:
        probe_spcs_str = ','.join(str(s) for s in probe_spcs[:20])  # sample up to 20
        df_probe = _read_sql_retry(QueryBuilder.probe_measurement_sets_query(probe_spcs_str), db)

        discovered_msets = df_probe['MEASUREMENT_SET_NAME'].dropna().unique().tolist()

        logger.info('=' * 60)
        logger.info('PROBE RESULTS — NCD MEASUREMENT_SET_NAME discovered:')
        for ms in discovered_msets:
            logger.info(f'  >> {ms}')
        if df_probe['ATTRIBUTES'].notna().any():
            # Prefer a GTOBE sample so we see real NCD CD attributes, not DUMMY calibration rows
            gtobe_rows = df_probe[df_probe['MEASUREMENT_SET_NAME'].str.contains('GTOBE', na=False)]
            sample_row = gtobe_rows if not gtobe_rows.empty else df_probe
            sample_attrs = sample_row['ATTRIBUTES'].dropna().iloc[0]
            logger.info(f'Sample ATTRIBUTES (from {"GTOBE" if not gtobe_rows.empty else "first"} set): {sample_attrs}')
        logger.info('=' * 60)

        if FIXED_NCD_MEASUREMENT_SET_NAME in discovered_msets:
            logger.info(
                f'Fixed measurement set confirmed in probe: {FIXED_NCD_MEASUREMENT_SET_NAME}'
            )
        else:
            logger.warning(
                f'Fixed measurement set NOT seen in probe sample: {FIXED_NCD_MEASUREMENT_SET_NAME}. '
                'Queries are still hard-filtered; chunk results may be empty if the set is absent.'
            )

        # Queries are now hard-filtered to FIXED_NCD_MEASUREMENT_SET_NAME.
        mset_filter_sql = f"m.MEASUREMENT_SET_NAME = '{FIXED_NCD_MEASUREMENT_SET_NAME}'"
        mset_filter_sql_stats = f"cp.MEASUREMENT_SET_NAME = '{FIXED_NCD_MEASUREMENT_SET_NAME}'"
        logger.info(f'Measurement set filter (allstats/meas): {mset_filter_sql}')
        logger.info(f'Measurement set filter (statistics):     {mset_filter_sql_stats}')

    # ── Step 6: Chunk processing ───────────────────────────────────────────────
    logger.info('─' * 60)
    logger.info(f'STEP 6 — Processing {len(lots)} lots in {len(lot_chunks)} chunks')
    sdtt_chunks = []

    for chunk_num, lot_chunk in enumerate(lot_chunks):
        logger.info(f'Processing chunk {chunk_num + 1}/{len(lot_chunks)} ({len(lot_chunk)} lots)')
        lot_chunk_str = ','.join(f"'{l}'" for l in lot_chunk)

        # Lot run card
        df_lrc = _read_sql_retry(QueryBuilder.lot_run_card_query(lot_chunk_str, mops_str), db)
        spcs = df_lrc['SPCS_ID'].drop_duplicates().tolist()
        if not spcs:
            logger.warning(f'Chunk {chunk_num + 1}: no SPCS IDs — skipping')
            continue
        spcs_id_str = ','.join(str(s) for s in spcs)
        logger.info(f'  {len(spcs)} SPCS IDs')

        # Allstats
        df_allstats_raw = _read_sql_retry(
            QueryBuilder.allstats_query(spcs_id_str, mset_filter_sql), db)
        logger.info(f'  Allstats raw: {len(df_allstats_raw)}')
        processor.df_to_csv(df_allstats_raw, f'ncd_allstats_raw_D1V_chunk{chunk_num+1}')

        measured_wafers = df_allstats_raw['WAFER_ID'].dropna().drop_duplicates().tolist()
        if not measured_wafers:
            logger.warning(f'Chunk {chunk_num + 1}: no wafers in allstats — skipping')
            continue
        wafer_chunk_str = ','.join(f"'{w}'" for w in measured_wafers)

        df_allstats_pivot = data_processor.process_allstats_data(df_allstats_raw, processor)
        del df_allstats_raw

        # Statistics — uses cp.MEASUREMENT_SET_NAME alias
        df_stats_raw = _read_sql_retry(
            QueryBuilder.statistics_query(spcs_id_str, mset_filter_sql_stats), db)
        logger.info(f'  Statistics raw: {len(df_stats_raw)}')
        processor.df_to_csv(df_stats_raw, f'ncd_statistics_raw_D1V_chunk{chunk_num+1}')
        df_stats_pivot = data_processor.process_statistics_data(df_stats_raw, processor)
        del df_stats_raw
        gc.collect()

        # WEC — main HM_ETCH chamber data
        df_wec_subop = _read_sql_retry(
            QueryBuilder.wec_query_optimized(lot_chunk_str, wec_op_str, wafer_chunk_str), db)
        logger.info(f'  WEC subop: {len(df_wec_subop)}')

        # WEC — SED (scanner/reticle)
        df_wec_sed = _read_sql_retry(
            QueryBuilder.wec_query_sed_only(sed_op_str, wafer_chunk_str), db) if sed_ops \
            else pd.DataFrame(columns=['WAFER_ID', 'SCANNER', 'SUBENTITY', 'OPERATION', 'RETICLE'])

        # WEC — ETCH (AME_ETCH / GTO_ETCH)
        df_wec_etch = _read_sql_retry(
            QueryBuilder.wec_query_etch_only(etch_op_str, wafer_chunk_str), db) if etch_ops \
            else pd.DataFrame(columns=['WAFER_ID', 'SCANNER', 'SUBENTITY', 'OPERATION'])

        df_wec_minimal = process_minimal_wec_data_separate(df_wec_sed, df_wec_etch, df_minimal_op_aliases)

        # Enhance main WEC with minimal (scanner/reticle/etch columns)
        df_wec_enhanced = pd.merge(df_wec_subop, df_wec_minimal, on='WAFER_ID', how='left')

        # Filter to Process-1 / Chuck-1 suboperations (same as HCCD flow)
        df_wec_filtered = df_wec_enhanced[
            df_wec_enhanced['SUB_OPERATION'].isin(['Process-1', 'Chuck-1'])
        ].copy()

        # Attach WEC alias for join
        df_wec_op_aliases_hm = df_wec_op_aliases[
            df_wec_op_aliases['ALIAS'].str.contains('HM_ETCH', na=False)
        ]
        df_wec_with_alias = pd.merge(df_wec_op_aliases_hm[['OPERATION', 'ALIAS']],
                                     df_wec_filtered, on='OPERATION', how='inner')
        df_wec_with_alias.rename(columns={'ALIAS': 'WEC_ALIAS'}, inplace=True)

        # Join all
        chunk_result = join_chunk_data(
            processor,
            df_allstats_pivot, df_stats_pivot,
            df_wec_with_alias, df_operalias,
            ncd_to_wec_alias_map=NCD_TO_WEC_HM_ALIAS_MAP,
        )

        # Merge SPC lot metadata
        spclot_keep = ['LOT7', 'LOT_TYPE', 'PRODUCT DEVREVSTEP', 'DATA_COLLECTION_TIME']
        df_spclot_renamed = df_spclot.rename(columns={
            c: 'SPC_' + c for c in df_spclot.columns if c not in spclot_keep
        })
        chunk_result['SPC_LOT'] = chunk_result['ALLSTATS_LOT'].copy()
        chunk_result['SPC_OPERATION'] = chunk_result['ALLSTATS_OPERATION'].copy()
        chunk_result = pd.merge(chunk_result, df_spclot_renamed,
                                on=['SPC_LOT', 'SPC_OPERATION'], how='inner')

        sdtt_chunks.append(chunk_result)
        logger.info(f'  Chunk {chunk_num + 1} result: {len(chunk_result)} rows '
                    f'(running total: {sum(len(c) for c in sdtt_chunks)})')

        del (df_allstats_pivot, df_stats_pivot,
             df_wec_subop, df_wec_sed, df_wec_etch, df_wec_enhanced,
             df_wec_filtered, df_wec_with_alias, chunk_result)
        gc.collect()

    if not sdtt_chunks:
        logger.warning('No data produced across all chunks. Output CSV will not be written.')
        return

    logger.info('─' * 60)
    logger.info(f'STEP 7 — Concatenating {len(sdtt_chunks)} chunks')
    SDTT = pd.concat(sdtt_chunks, ignore_index=True)
    del sdtt_chunks
    gc.collect()
    logger.info(f'Combined: {len(SDTT)} rows')

    # ── Post-processing transforms ────────────────────────────────────────────
    logger.info('STEP 8 — Post-processing transforms')
    add_esc_zones(SDTT)
    rename_final_columns(SDTT)
    add_derived_columns(SDTT)
    SDTT = reorder_columns(SDTT)
    cleanup_and_sort(SDTT)

    # ── Merge with existing output CSV (rolling append + dedup) ───────────────
    logger.info('STEP 9 — Merge with existing output and write')
    out_name = config['output_csv_name']
    df_existing = processor.main_csv_to_df(out_name)

    if not df_existing.empty:
        logger.info(f'Existing CSV has {len(df_existing)} rows — deduplicating on WAFER_ID + TEST_NAME')
        dup_keys = SDTT[['WAFER_ID', 'TEST_NAME']].drop_duplicates()
        df_existing_filtered = (
            df_existing
            .merge(dup_keys, on=['WAFER_ID', 'TEST_NAME'], how='left', indicator=True)
            .query('_merge == "left_only"')
            .drop(columns='_merge')
            .reset_index(drop=True)
        )
        logger.info(f'Removed {len(df_existing) - len(df_existing_filtered)} duplicates from existing')
        SDTT_final = pd.concat([df_existing_filtered, SDTT], ignore_index=True)
    else:
        SDTT_final = SDTT

    # Sort by time descending
    if 'DATA_COLLECTION_TIME' in SDTT_final.columns:
        SDTT_final['DATA_COLLECTION_TIME'] = pd.to_datetime(
            SDTT_final['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')
        SDTT_final.sort_values('DATA_COLLECTION_TIME', ascending=False, inplace=True)
        SDTT_final.reset_index(drop=True, inplace=True)

    processor.main_df_to_csv(SDTT_final, out_name, no_index=1)
    logger.info('=' * 80)
    logger.info(f'OUTPUT: integrated_output/{out_name}.csv  ({len(SDTT_final)} rows total)')
    logger.info(f'MEASUREMENT_SET_NAME fixed: {FIXED_NCD_MEASUREMENT_SET_NAME}')
    logger.info(f'MONITOR_SET_NAME fixed:     {FIXED_NCD_MONITOR_SET_NAME}')
    logger.info(f'MEASUREMENT_SET_NAMES discovered in probe: {discovered_msets}')
    logger.info('1278 NCD MT5+MT6 D1V Pipeline — COMPLETE')
    logger.info('=' * 80)


if __name__ == '__main__':
    main()
