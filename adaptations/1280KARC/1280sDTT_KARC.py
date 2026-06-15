# -*- coding: utf-8 -*-
"""
1280 sDTT Data Processing Module — D1V Only
============================================
Adapted from 1278sDTT_D1V_F32.py for the 1280 process node.

Key differences from 1278:
  - DATA_SOURCE: 'P1280' (not 'D1_P1278')
  - LOT_PROCESS: '1280'
  - tech1 alias prefix: "" (empty — e.g. E_M6_HM_ETCH, not E_8M6_HM_ETCH)
  - tech2 meas-set suffix: "80" (e.g. CD.FCCD_ALLSTATS.80)
  - Layer range: M6–M16, no BM0
  - Sites: D1V only
  - CD/LAYER slice positions: [5:9] / [10:13]
  - PILOT_NAME may be absent — guarded with column check
  - statistics_query uses string_contains() UDF (not LIKE chains)
  - No APC join

Usage — standalone:
  python 1280sDTT_D1V.py

Usage — via pipeline:
  import this module via 1280sDTT_PIPELINE.py
"""

import gc
import json
import os
import time
import argparse
import pandas as pd
import numpy as np
import PyUber
from datetime import date
import warnings
import logging
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────
# layerRange and incBM0 are authoritative in PIPELINE_CONFIG when run via
# the pipeline orchestrator; kept here as fallback defaults for standalone runs.
KARC_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    'ceid': 'KARC',
    'sites': ['D1V'],

    'database_connections': {
        'D1V': 'D1D_PROD_XEUS_LOCAL',
    },

    'tech': "1280",
    'structure': "NEST",
    'structures': ['NEST', 'NESTVIA_PR', 'NESTVIA_HM', 'TRENCH', 'ETE'],
    'tech_alias_nums': {"1278": "8", "1280": ""},
    # KARC scope: MT1 and MT2 only
    'layerRange': [1, 1],
    'incBM0': 0,
    'days': 40,
    'nLots_chunk': 25,
    # debug_writes is authoritative in PIPELINE_CONFIG; True here for standalone runs.
    'debug_writes': False,
    'folder_path': os.path.join(KARC_DIR, 'debug'),
    'main_csv_path': os.path.join(KARC_DIR, 'integrated_output'),
    'main_csv_base_name': '1280sDTT_KARC',
    'cd_levels': ['FCCD'],
    'cd_alias_levels': ['FCCD'],
    # Disable raw-measurement query/join by default to prevent row multiplication.
    'include_raw_measurements': False,
    # Operation filtering control:
    # - alias-driven: current behavior (alias lookup + configured fallbacks)
    # - explicit-operations: bypass alias lookup and use explicit operation lists below
    'operation_filtering_mode': 'alias-driven',
    'explicit_operations': {
        'spc': ['270387'],
        'wec': ['269250'],
    },
    # Pull FCCD sets first and derive STRUCTURE in processing; avoids over-filtering sparse MT2 runs.
    'use_structure_sql_filter': False,
    # If strict WEC alias lookup returns no rows, retry with a relaxed alias match.
    'enable_wec_alias_relaxed_fallback': True,
    # Direct OPERATION overrides for missing aliases (e.g., alias not present in F_OPERATION_ALIAS).
    'wec_operation_overrides': {
        'E_V0_MAIN_ETCH': ['269250'],
        'E_V1_MAIN_ETCH': ['269250'],
    },
    # If alias-based WEC operation resolution is empty, query WEC by lot+wafer only.
    'enable_wec_noop_fallback': True,
    'suppress_sqlalchemy_warnings': True,
    'log_level': 'INFO',
}

# Set pandas display options
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)
pd.set_option('display.max_rows', None)


def setup_logging(log_level='INFO'):
    """Configure logging for the application.

    If the root logger already has handlers (e.g. this script was imported by
    the pipeline orchestrator which configured logging itself), skip
    re-initialisation entirely.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return logging.getLogger(__name__)

    # ── Standalone execution: configure from scratch ──────────────────────
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = os.path.join(log_dir, f'sdtt_1280_processing_{timestamp}.log')

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler(),
        ],
    )

    for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
                   'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Log file: {log_filename}")
    return logger


def setup_warning_filters(suppress_sqlalchemy=True):
    """Configure warning filters for cleaner console output."""
    logger = logging.getLogger(__name__)
    if suppress_sqlalchemy:
        warnings.filterwarnings('ignore',
                                message='.*pandas only supports SQLAlchemy connectable.*',
                                category=UserWarning)
        logger.info("SQLAlchemy connection warnings suppressed.")


# ══════════════════════════════════════════════════════════════════════════════
class SDTTProcessor:
    def __init__(self, config, site):
        self.config = config
        self.site = site
        self.database_connection = config['database_connections'][site]
        self.logger = logging.getLogger(__name__)
        os.makedirs(config['folder_path'], exist_ok=True)
        os.makedirs(config['main_csv_path'], exist_ok=True)
        self.setup_paths()

    def setup_paths(self):
        self.folder_path = self.config['folder_path']
        self.main_csv_path = self.config['main_csv_path']
        self.logger.debug(f"Paths initialized — Working: {self.folder_path}, Main: {self.main_csv_path}")

    def main_df_to_csv(self, df, name, no_index=None, show=None):
        csvwritefile = os.path.join(self.main_csv_path, f'{name}.csv')
        if no_index == 1:
            df.to_csv(csvwritefile, index=False)
        else:
            df.to_csv(csvwritefile)
        self.logger.debug(f"Saved DataFrame to main CSV: {csvwritefile} ({len(df)} rows)")
        if show == 1:
            os.startfile(csvwritefile)

    def main_csv_to_df(self, name):
        csvreadfile = os.path.join(self.main_csv_path, f'{name}.csv')
        if os.path.exists(csvreadfile):
            df = pd.read_csv(csvreadfile)
            self.logger.info(f"Loaded existing CSV: {csvreadfile} ({len(df)} rows)")
            return df
        else:
            self.logger.info(f"CSV file {csvreadfile} does not exist. Creating new dataset.")
            return pd.DataFrame()

    def df_to_csv(self, df, name, no_index=None, show=None):
        """Save DataFrame to working folder.  Skipped when debug_writes=False."""
        if not self.config.get('debug_writes', True):
            return
        csvwritefile = os.path.join(self.folder_path, f'{name}.csv')
        if no_index == 1:
            df.to_csv(csvwritefile, index=False)
        else:
            df.to_csv(csvwritefile)
        self.logger.debug(f"Saved DataFrame to working folder: {name}.csv ({len(df)} rows)")
        if show == 1:
            os.startfile(csvwritefile)

    def csv_to_df(self, name):
        csvreadfile = os.path.join(self.folder_path, f'{name}.csv')
        df = pd.read_csv(csvreadfile)
        self.logger.debug(f"Loaded DataFrame from working folder: {name}.csv ({len(df)} rows)")
        return df


# ══════════════════════════════════════════════════════════════════════════════
class QueryBuilder:
    """Centralised SQL query management for 1280 D1V."""

    @staticmethod
    def operalias_query(all_cd_aliases_str, site):
        return f"""SELECT '{site}' "SITE"
          ,a1.DATA_SOURCE
          ,a1.OPERATION
          ,a1.OPER_GROUP_KEY "GROUP_KEY"
          ,a1.OPER_GROUP_NAME "ALIAS"
          ,a1.OPER_INTEGRATION_LAYER "LAYER"
          ,a1.OPER_SHORT_DESC
          ,a1.OPER_LONG_DESC
        FROM F_OPERATION_ALIAS a1
        WHERE a1.DATA_SOURCE IN ('P1280')
          AND UPPER(a1.OPER_GROUP_NAME) IN ({all_cd_aliases_str})"""

    @staticmethod
    def spclot_prefetch_query(days, mops_str, site, layer=None):
        layer_filter = ''
        if layer is not None:
            # Support both MTx and legacy Mx naming, with ST/non-ST variants and optional trailing H.
            layer_filter = f"""
        AND (
             UPPER(l.TEST_NAME) LIKE '%.FCCD.MT{layer}'
          OR UPPER(l.TEST_NAME) LIKE '%.FCCD.MT{layer}H'
          OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.MT{layer}'
          OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.MT{layer}H'
          OR UPPER(l.TEST_NAME) LIKE '%.FCCD.M{layer}'
          OR UPPER(l.TEST_NAME) LIKE '%.FCCD.M{layer}H'
          OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.M{layer}'
          OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.M{layer}H'
        )
        """
        return f"""
        SELECT '{site}' "SITE"
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
        WHERE l.DATA_COLLECTION_TIME>=TRUNC(CURRENT_DATE)-{days}
        AND l.OPERATION IN ({mops_str})
        AND l.LOT_PROCESS='1280'
        {layer_filter}
        """

    @staticmethod
    def lot_run_card_query(spc_lot_str, mops_str, layer=None):
        layer_filter = ''
        if layer is not None:
            layer_filter = f"""
          AND (
               UPPER(l.TEST_NAME) LIKE '%.FCCD.MT{layer}'
            OR UPPER(l.TEST_NAME) LIKE '%.FCCD.MT{layer}H'
            OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.MT{layer}'
            OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.MT{layer}H'
            OR UPPER(l.TEST_NAME) LIKE '%.FCCD.M{layer}'
            OR UPPER(l.TEST_NAME) LIKE '%.FCCD.M{layer}H'
            OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.M{layer}'
            OR UPPER(l.TEST_NAME) LIKE '%.ST.FCCD.M{layer}H'
          )
            """
        return f"""
        SELECT l.LOT "LOT"
          ,l.OPERATION
          ,l.DATA_COLLECTION_TIME
          ,TO_CHAR(CAST(l.SPCS_ID AS DECIMAL(20,10))) "SPCS_ID"
          ,l.SPC_DATA_ID
        FROM F_LOT_RUN_CARD h
        INNER JOIN P_SPC_LOT l
          ON  l.LOTOPERKEY=h.LOTOPERKEY
          AND l.MONITOR_TYPE='WIP MONITOR'
        INNER JOIN P_SPC_SESSION s
          ON  s.SPCS_ID=l.SPCS_ID
          AND s.LATEST_FLAG='Y'
        WHERE h.LOT IN ({spc_lot_str})
          AND h.OPERATION IN ({mops_str})
                    {layer_filter}
        """

    @staticmethod
    def spc_measurements_no_attr_split_query(spcs_id_str, structure_list=None, use_structure_filter=True):
        if structure_list is None:
            structure_list = ['NEST']
        # STRUCTURE can be encoded in ATTRIBUTES or PARAMETERS, with or without trailing ';'.
        structure_or = ' OR '.join([
            "(" 
            f"UPPER(NVL(a.ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()};%' OR "
            f"UPPER(NVL(a.ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()}%' OR "
            f"UPPER(NVL(a.PARAMETERS,'')) LIKE '%STRUCTURE={s.upper()};%' OR "
            f"UPPER(NVL(a.PARAMETERS,'')) LIKE '%STRUCTURE={s.upper()}%'"
            ")"
            for s in structure_list
        ])
        structure_clause = f"AND ({structure_or})" if use_structure_filter else ""
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
          ON  m.SPCS_ID=s.SPCS_ID
          AND m.STATUS NOT IN ('M','I')
        INNER JOIN P_SPC_MEASUREMENT_ATTRIBUTE a
          ON a.ATTRIBUTE_ID=m.ATTRIBUTE_ID
        LEFT JOIN F_WAFERENTITYHIST w
          ON  w.BATCH_ID=s.BATCH_ID
          AND w.WAFER=m.WAFER
        LEFT JOIN F_LOT_WAFER_RECIPE r
          ON  r.RECIPE_ID=w.WAFER_RECIPE_ID
        WHERE s.SPCS_ID IN ({spcs_id_str})
                      AND UPPER(m.MEASUREMENT_SET_NAME) LIKE 'CD.%FCCD%MEASUREMENTS%'
                    {structure_clause}
        """

    @staticmethod
    def allstats_query(spcs_id_str, structure_list=None, use_structure_filter=True):
        if structure_list is None:
            structure_list = ['NEST']
        # STRUCTURE can be encoded in ATTRIBUTES or PARAMETERS, with or without trailing ';'.
        structure_or = ' OR '.join([
            "(" 
            f"UPPER(NVL(a.ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()};%' OR "
            f"UPPER(NVL(a.ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()}%' OR "
            f"UPPER(NVL(a.PARAMETERS,'')) LIKE '%STRUCTURE={s.upper()};%' OR "
            f"UPPER(NVL(a.PARAMETERS,'')) LIKE '%STRUCTURE={s.upper()}%'"
            ")"
            for s in structure_list
        ])
        structure_clause = f"AND ({structure_or})" if use_structure_filter else ""
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
          ON  m.SPCS_ID=s.SPCS_ID
          AND m.STATUS NOT IN ('M','I')
        INNER JOIN P_SPC_MEASUREMENT_ATTRIBUTE a
          ON a.ATTRIBUTE_ID=m.ATTRIBUTE_ID
        WHERE s.SPCS_ID IN ({spcs_id_str})
                      AND UPPER(m.MEASUREMENT_SET_NAME) LIKE 'CD.%FCCD%ALLSTATS%'
                    {structure_clause}
        """

    @staticmethod
    def process_op_aliases_query(all_wec_aliases_str, site):
        return f"""
        SELECT '{site}' "SITE"
            ,a1.DATA_SOURCE
            ,a1.OPERATION
            ,a1.OPER_GROUP_KEY "GROUP_KEY"
            ,a1.OPER_GROUP_NAME "ALIAS"
            ,a1.OPER_INTEGRATION_LAYER "LAYER"
            ,a1.OPER_SHORT_DESC
            ,a1.OPER_LONG_DESC
        FROM F_OPERATION_ALIAS a1
        WHERE a1.DATA_SOURCE IN ('P1280')
            AND UPPER(TRIM(a1.OPER_GROUP_NAME)) IN ({all_wec_aliases_str})
        """

    @staticmethod
    def process_op_aliases_query_relaxed(all_wec_aliases, site):
        alias_predicates = ' OR '.join([
            f"UPPER(NVL(a1.OPER_GROUP_NAME,'')) LIKE '%{alias.upper()}%'"
            for alias in all_wec_aliases
        ])
        return f"""
        SELECT '{site}' "SITE"
            ,a1.DATA_SOURCE
            ,a1.OPERATION
            ,a1.OPER_GROUP_KEY "GROUP_KEY"
            ,a1.OPER_GROUP_NAME "ALIAS"
            ,a1.OPER_INTEGRATION_LAYER "LAYER"
            ,a1.OPER_SHORT_DESC
            ,a1.OPER_LONG_DESC
        FROM F_OPERATION_ALIAS a1
        WHERE ({alias_predicates})
            AND a1.OPERATION IS NOT NULL
        """

    @staticmethod
    def wec_query_optimized(spc_lot_str, wec_op_str, wafer_chunk_str, site):
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
        ,SUBSTR(c.ENTITY,1,3) "ENTITY_PREFIX"
        ,TO_CHAR(CAST(H.RUNKEY AS DECIMAL(20,10))) "RUNKEY"
        ,c.CHAMBER_PROCESS_DURATION "PROCESS_TIME"
        ,c.CHAMBER_PROCESS_ORDER "PROCESS_ORDER"
        ,c.CHAMBER_WAIT_DURATION "WAIT_TIME"
        ,CAST(h.LAST_TXN_TIME AS DATE) "LAST_TXN_TIME"
        ,c.SLOT
        ,NVL(h.ENTITY_OWNED_BY,'{site}') "ENTITY_OWNED_BY"
        ,r.RECIPE
        FROM F_LOT_RUN_MAP h
        INNER JOIN F_WAFERENTITYHIST w
        ON  w.RUNKEY=h.RUNKEY
        AND w.EXPECTED_LOT=h.EXPECTED_LOT
        AND w.WAFER IS NOT NULL
        AND w.IS_CONDITIONING_WAFER IS NULL
        INNER JOIN F_WAFERCHAMBERHIST c
        ON  c.RUNKEY=w.RUNKEY
        AND c.WAFER=w.WAFER
        AND c.ENTITY=w.ENTITY
        INNER JOIN F_LOT_WAFER_RECIPE r
        ON  r.RECIPE_ID=w.WAFER_RECIPE_ID
        WHERE h.EXPECTED_LOT IN ({spc_lot_str})
        AND h.OPERATION IN ({wec_op_str})
        AND c.OPERATION IN ({wec_op_str})
        AND c.WAFER IN ({wafer_chunk_str})"""

    @staticmethod
    def wec_query_noop_fallback(spc_lot_str, wafer_chunk_str, site):
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
        ,SUBSTR(c.ENTITY,1,3) "ENTITY_PREFIX"
        ,TO_CHAR(CAST(H.RUNKEY AS DECIMAL(20,10))) "RUNKEY"
        ,c.CHAMBER_PROCESS_DURATION "PROCESS_TIME"
        ,c.CHAMBER_PROCESS_ORDER "PROCESS_ORDER"
        ,c.CHAMBER_WAIT_DURATION "WAIT_TIME"
        ,CAST(h.LAST_TXN_TIME AS DATE) "LAST_TXN_TIME"
        ,c.SLOT
        ,NVL(h.ENTITY_OWNED_BY,'{site}') "ENTITY_OWNED_BY"
        ,r.RECIPE
        FROM F_LOT_RUN_MAP h
        INNER JOIN F_WAFERENTITYHIST w
        ON  w.RUNKEY=h.RUNKEY
        AND w.EXPECTED_LOT=h.EXPECTED_LOT
        AND w.WAFER IS NOT NULL
        AND w.IS_CONDITIONING_WAFER IS NULL
        INNER JOIN F_WAFERCHAMBERHIST c
        ON  c.RUNKEY=w.RUNKEY
        AND c.WAFER=w.WAFER
        AND c.ENTITY=w.ENTITY
        INNER JOIN F_LOT_WAFER_RECIPE r
        ON  r.RECIPE_ID=w.WAFER_RECIPE_ID
        WHERE h.EXPECTED_LOT IN ({spc_lot_str})
        AND c.WAFER IN ({wafer_chunk_str})"""

    @staticmethod
    def statistics_query(spcs_id_str, structure_list=None, use_structure_filter=True):
        # Uses string_contains() UDF (1280 XEUS DB) instead of LIKE chains.
        # DENSE_RANK partition includes c.CHART_PARAMETER for 1280 granularity.
        if structure_list is None:
            structure_list = ['NEST']
        # STRUCTURE matching tolerant to payload differences and casing.
        structure_conditions = ' OR '.join([
            f"UPPER(NVL(c.CHART_ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()};%' OR "
            f"UPPER(NVL(c.CHART_ATTRIBUTES,'')) LIKE '%STRUCTURE={s.upper()}%'"
            for s in structure_list
        ])
        structure_clause = f"AND ({structure_conditions})" if use_structure_filter else ""
        return f"""
        SELECT x.*
        FROM
        (
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
        ,NVL(cp.WAFER,w.WAFER) "WAFER_ID"
        ,cl.CENTERLINE
        ,cl.TARGET
        ,cl.LO_CONTROL_LMT "LCL"
        ,cl.UP_CONTROL_LMT "UCL"
        ,cl.LO_DISPOSITION_LMT "LDL"
        ,cl.UP_DISPOSITION_LMT "UDL"
        ,cl.LO_SPEC_LMT "LSL"
        ,cl.UP_SPEC_LMT "USL"
        ,DENSE_RANK() OVER (PARTITION BY l.LOT, l.OPERATION, cp.MEASUREMENT_SET_NAME, cp.CHART_TYPE, c.CHART_PARAMETER ORDER BY cp.DATA_COLLECTION_TIME DESC) "PASS_ORDER"
        FROM P_SPC_CHART_POINT cp
        INNER JOIN P_SPC_LOT l
        ON  l.SPCS_ID=cp.SPCS_ID
        INNER JOIN P_SPC_CHART c
        ON c.CHART_ID=cp.CHART_ID
        LEFT JOIN P_SPC_CHART_LIMIT cl
        ON  cp.CHART_ID= cl.CHART_ID
        AND cp.LIMIT_ID= cl.LIMIT_ID
        LEFT JOIN P_SPC_CHARTPOINT_WAFER w
        ON  w.SPCS_ID=cp.SPCS_ID
        AND w.CHART_ID=cp.CHART_ID
        AND w.CHART_POINT_SEQ=cp.CHART_POINT_SEQ
        WHERE cp.SPCS_ID IN ({spcs_id_str})
                      AND UPPER(cp.MEASUREMENT_SET_NAME) LIKE 'CD.%FCCD%STATISTICS%'
                    {structure_clause}
          AND NVL(cp.WAFER,w.WAFER) IS NOT NULL
        ) x
        WHERE x.PASS_ORDER=1
        /* SPCM-HINT-AN */
        """


# ══════════════════════════════════════════════════════════════════════════════
class DataProcessor:
    """Handle data processing operations."""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def parse_attributes(attr_string):
        if not isinstance(attr_string, str) or not attr_string:
            return {}
        attributes = {}
        pairs = attr_string.strip(';').split(';')
        for pair in pairs:
            if '=' in pair:
                variable, value = pair.split('=', 1)
                attributes[variable] = value
        return attributes

    @staticmethod
    def extract_cd_layer(test_name_series, measurement_set_series=None):
        """Extract CD/LAYER from TEST_NAME with fallbacks for naming drift."""
        test_names = test_name_series.fillna('').astype(str)

        if measurement_set_series is not None:
            meas_sets = measurement_set_series.fillna('').astype(str)
        else:
            meas_sets = pd.Series('', index=test_names.index)

        # Supports BM0, legacy Mxx, and newer MTx patterns, with optional trailing H.
        layer = test_names.str.extract(r'(?i)\.((?:BM0)|(?:M(?:T)?\d{1,2}))H?$', expand=False)

        # CD is derived from TEST_NAME first, then falls back to measurement set.
        cd = test_names.str.extract(r'(?i)\.([FDH]CCD)\.', expand=False)
        cd = cd.fillna(meas_sets.str.extract(r'(?i)\.([FDH]CCD)_', expand=False))
        cd = cd.str.upper()
        layer = layer.str.upper()

        # Historical HCCD convention: trailing H can encode HCCD from FCCD pattern.
        # Keep this restricted to FCCD to avoid accidental remapping of other CD families.
        h_mask = test_names.str.endswith('H', na=False) & cd.eq('FCCD')
        cd = cd.where(~h_mask, 'H' + cd.str[1:])

        return cd.fillna('UNKNOWN'), layer.fillna('')

    @staticmethod
    def chunk_list(spcs, chunk_length):
        if not isinstance(spcs, list):
            raise TypeError("Input 'spcs' must be a list.")
        if not isinstance(chunk_length, int) or chunk_length <= 0:
            raise ValueError("Input 'chunk_length' must be a positive integer.")
        result = []
        for i in range(0, len(spcs), chunk_length):
            result.append(spcs[i:i + chunk_length])
        return result

    @staticmethod
    def get_layerList(a, b=None):
        if b is None:
            return [a]
        else:
            if a > b:
                return []
            return list(range(a, b + 1))

    def process_measurements_data(self, df_raw, processor):
        """Process SPC measurements data with pivot.

        1280 CD/LAYER slice positions: [5:9] / [10:13]
        (confirmed against 1280sDTT.py)
        """
        self.logger.info(f"Processing measurements data: {len(df_raw)} raw records")

        base_cols = ['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE']
        if df_raw.empty:
            self.logger.warning("Measurements query returned 0 rows; returning empty frame with join keys")
            return pd.DataFrame(columns=base_cols + ['CD', 'LAYER'])

        required_cols = ['ATTRIBUTES', 'WAFER_X', 'WAFER_Y', 'VALUE']
        missing_cols = [c for c in required_cols if c not in df_raw.columns]
        if missing_cols:
            self.logger.warning(f"Measurements data missing columns {missing_cols}; skipping pivot for this chunk")
            available = [c for c in ['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE'] if c in df_raw.columns]
            fallback = df_raw[available].drop_duplicates() if available else pd.DataFrame(columns=base_cols)
            for col in base_cols:
                if col not in fallback.columns:
                    fallback[col] = ''
            fallback = fallback.reindex(columns=base_cols)
            fallback['CD'], fallback['LAYER'] = DataProcessor.extract_cd_layer(
                fallback['TEST_NAME'],
                fallback['MEASUREMENT_SET_NAME'] if 'MEASUREMENT_SET_NAME' in fallback.columns else None,
            )
            return fallback

        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('ATTRIBUTES', axis=1)
        processor.df_to_csv(df_attr_split, 'measurements_attr_split_no_pivot')

        df_attr_split['WAFER_RADIUS'] = np.sqrt(
            df_attr_split['WAFER_X']**2 + df_attr_split['WAFER_Y']**2)

        df_attr_split['WAFER_RECIPE'] = df_attr_split['WAFER_RECIPE'].fillna('MISSING')

        # Some products provide MEASURE_INDEX in PARAMETERS instead of ATTRIBUTES.
        if 'MEASURE_INDEX' not in df_attr_split.columns and 'PARAMETERS' in df_attr_split.columns:
            params = df_attr_split['PARAMETERS'].fillna('').astype(str)
            measure_idx = params.str.extract(r'(?i)(?:^|;)MEASURE_INDEX=([^;]+)', expand=False)
            if measure_idx.isna().all():
                measure_idx = params.str.extract(r'(?i)(?:^|;)MEASUREINDEX=([^;]+)', expand=False)
            if measure_idx.isna().all():
                measure_idx = params.str.extract(r'(?i)(?:^|;)POINT_INDEX=([^;]+)', expand=False)
            if not measure_idx.isna().all():
                df_attr_split['MEASURE_INDEX'] = measure_idx

        if 'MEASURE_INDEX' not in df_attr_split.columns:
            self.logger.warning("Measurements data missing MEASURE_INDEX after parsing; skipping pivot for this chunk")
            fallback = df_attr_split[['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE']].drop_duplicates()
            fallback['CD'], fallback['LAYER'] = DataProcessor.extract_cd_layer(
                fallback['TEST_NAME'],
                fallback['MEASUREMENT_SET_NAME'] if 'MEASUREMENT_SET_NAME' in fallback.columns else None,
            )
            return fallback

        df_pivot = df_attr_split.pivot_table(
            index=['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE'],
            columns='MEASURE_INDEX',
            values=['WAFER_RADIUS', 'VALUE'],
            aggfunc='first')

        df_pivot.columns = ['_'.join(col).strip() for col in df_pivot.columns.values]
        df_pivot.reset_index(inplace=True)

        # Parse CD/LAYER with regex to support both legacy and newer TEST_NAME patterns.
        df_pivot['CD'], df_pivot['LAYER'] = DataProcessor.extract_cd_layer(
            df_pivot['TEST_NAME'],
            df_pivot['MEASUREMENT_SET_NAME'] if 'MEASUREMENT_SET_NAME' in df_pivot.columns else None,
        )

        self.logger.info(f"Measurements data processed: {len(df_pivot)} pivoted records")
        return df_pivot

    def process_allstats_data(self, df_raw, processor):
        """Process allstats data with pivot.

        PILOT_NAME guard: 1280 often lacks this column in allstats results.
        """
        self.logger.info(f"Processing allstats data: {len(df_raw)} raw records")

        common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID', 'STRUCTURE']
        if df_raw.empty:
            self.logger.warning("Allstats query returned 0 rows; returning empty frame with join keys")
            return pd.DataFrame(columns=common_cols)

        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop(['ATTRIBUTES', 'PARAMETERS', 'MEASUREMENT_ID'], axis=1)

        if 'STRUCTURE' in df_attr_split.columns:
            structure_counts = df_attr_split['STRUCTURE'].fillna('MISSING').astype(str).value_counts().head(8).to_dict()
            self.logger.info(f"Allstats STRUCTURE mix (top): {structure_counts}")

        # 1280 PILOT_NAME guard — column is often absent in allstats results
        if 'PILOT_NAME' in df_attr_split.columns:
            df_attr_split['PILOT_NAME'] = df_attr_split['PILOT_NAME'].fillna('MISSING')
        else:
            df_attr_split['PILOT_NAME'] = 'MISSING'

        pivot_index = ['WAFER_ID', 'SPCS_ID', 'DYNWAFER', 'IS_POR', 'STRUCTURE']
        pivot_column = ['CD_TERMS']
        pivot_values = ['VALUE']

        if 'CD_TERMS' not in df_attr_split.columns:
            self.logger.warning("Allstats data missing CD_TERMS; skipping pivot for this chunk")
            existing = [c for c in common_cols if c in df_attr_split.columns]
            if len(existing) == len(common_cols):
                return df_attr_split[common_cols].drop_duplicates()
            return pd.DataFrame(columns=common_cols)

        df_pivot_nomerge = df_attr_split.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first')

        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(col).strip() for col in df_pivot_nomerge.columns.values]
        df_pivot_nomerge.reset_index(inplace=True)

        df_merge_cols = df_attr_split.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='inner')

        self.logger.info(f"Allstats data processed: {len(df_pivot)} pivoted records")
        return df_pivot

    def process_statistics_data(self, df_raw, processor):
        """Process statistics data with pivot."""
        self.logger.info(f"Processing statistics data: {len(df_raw)} raw records")

        common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID', 'STRUCTURE']
        if df_raw.empty:
            self.logger.warning("Statistics query returned 0 rows; returning empty frame with join keys")
            return pd.DataFrame(columns=common_cols)

        parsed_data = df_raw['CHART_ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('CHART_ATTRIBUTES', axis=1)

        pivot_index = ['WAFER_ID', 'SPCS_ID', 'STRUCTURE']
        pivot_column = ['CD_TERMS']
        pivot_values = ['VALUE', 'CENTERLINE', 'TARGET', 'LCL', 'UCL', 'LDL', 'UDL', 'LSL', 'USL',
                        'VALID_FLAG', 'STANDARD_FLAG', 'CORRECTED_FLAG', 'INCONTROL_FLAG',
                        'INDISPOSITION_FLAG', 'VIOLATED_RULE_NOTATION', 'CHART_ID', 'CHART_TYPE',
                        'CHART_POINT_SEQ']

        if 'CD_TERMS' not in df_attr_split.columns:
            self.logger.warning("Statistics data missing CD_TERMS; skipping pivot for this chunk")
            existing = [c for c in common_cols if c in df_attr_split.columns]
            if len(existing) == len(common_cols):
                return df_attr_split[common_cols].drop_duplicates()
            return pd.DataFrame(columns=common_cols)

        df_pivot_nomerge = df_attr_split.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first')

        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(col).strip() for col in df_pivot_nomerge.columns.values]
        df_pivot_nomerge.reset_index(inplace=True)

        df_merge_cols = df_attr_split.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='outer')

        self.logger.info(f"Statistics data processed: {len(df_pivot)} pivoted records")
        return df_pivot


# ══════════════════════════════════════════════════════════════════════════════
def main(config=None):
    """Entry point for the 1280 SPC/WEC data pipeline.

    Parameters
    ----------
    config : dict, optional
        When None (default) the module-level CONFIG dict is used so the script
        runs unchanged when executed directly.
    """
    if config is None:
        config = CONFIG

    logger = setup_logging(config.get('log_level', 'INFO'))

    logger.info("=" * 80)
    logger.info("1280 SDTT Data Processing Script Started")
    logger.info("=" * 80)
    logger.info(f"Configuration: {config}")

    setup_warning_filters(config.get('suppress_sqlalchemy_warnings', True))

    tech1 = config['tech_alias_nums'][config['tech']]  # "" for 1280
    layerList = DataProcessor.get_layerList(
        config['layerRange'][0],
        config['layerRange'][1] if len(config['layerRange']) > 1 else None)
    # No BM0 for 1280 — incBM0 is always 0

    logger.info(f"Processing layers: {layerList}")
    logger.info(f"Tech alias prefix: '{tech1}', Days: {config['days']}, Chunk size: {config['nLots_chunk']}")

    for site in config['sites']:
        logger.info("=" * 80)
        logger.info(f"Processing Site: {site}")
        logger.info(f"Database Connection: {config['database_connections'][site]}")
        logger.info("=" * 80)

        processor = SDTTProcessor(config, site)
        data_processor = DataProcessor()

        site_cd_temp_files = {}

        # ── Checkpoint / resume support ───────────────────────────────────
        resume = config.get('resume', False)
        checkpoint_path = os.path.join(processor.folder_path, f'sdtt_1280_checkpoint_{site}.json')
        completed_layers = []

        if resume and os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, 'r') as _cf:
                    _ckpt = json.load(_cf)
                if (    _ckpt.get('days')       == config['days']
                    and sorted(_ckpt.get('cd_levels', [])) == sorted(config['cd_levels'])
                    and _ckpt.get('layerRange') == config['layerRange']
                    and _ckpt.get('incBM0')     == config['incBM0']):
                    completed_layers = _ckpt.get('completed_layers', [])
                    logger.info(f"Resuming {site}: {len(completed_layers)} layers already done: {completed_layers}")
                else:
                    logger.warning(
                        f"Checkpoint config mismatch for {site} — starting fresh.\n"
                        f"  Checkpoint: days={_ckpt.get('days')}, cd_levels={_ckpt.get('cd_levels')}, "
                        f"layerRange={_ckpt.get('layerRange')}, incBM0={_ckpt.get('incBM0')}\n"
                        f"  Current:    days={config['days']}, cd_levels={config['cd_levels']}, "
                        f"layerRange={config['layerRange']}, incBM0={config['incBM0']}"
                    )
            except Exception as _ce:
                logger.warning(f"Could not read checkpoint for {site}: {_ce} — starting fresh")
        elif resume:
            logger.info(f"Resume requested for {site} but no checkpoint found — starting fresh")

        for cd_level in config['cd_levels']:
            temp_path = os.path.join(processor.folder_path,
                                     f'sdtt_1280_site_{site}_{cd_level}_temp.csv')
            site_cd_temp_files[cd_level] = temp_path
            if resume and completed_layers:
                if os.path.exists(temp_path):
                    logger.info(f"Keeping existing temp CSV for resume: {os.path.basename(temp_path)}")
            else:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        for index0, layer in enumerate(layerList):
            logger.info("=" * 60)
            logger.info(f"Processing Site {site} - Layer {index0+1} of {len(layerList)}: {layer}")
            logger.info("=" * 60)

            if layer in completed_layers:
                logger.info(f"  [RESUME] Layer {layer} already completed — skipping")
                continue

            # KARC alias generation — restrict to configured CD levels (FCCD only by default)
            wec_map = {
                "FCCD": "E_{}V{}_MAIN_ETCH",
            }
            wec_layers_map = {"FCCD": [layer - 1]}
            cd_layers_map  = {"FCCD": [layer]}
            selected_cd_levels = [cd for cd in config.get('cd_levels', ['FCCD']) if cd in wec_map]

            all_wec_aliases = []
            all_cd_aliases  = []
            mop_temp = "L_{}M{}_{}"

            for cd in selected_cd_levels:
                for wec_layer in wec_layers_map[cd]:
                    all_wec_aliases.append(wec_map[cd].format(tech1, wec_layer))
                for cd_layer in cd_layers_map[cd]:
                    all_cd_aliases.append(mop_temp.format(tech1, cd_layer, cd))

            all_cd_aliases_str  = ','.join(f"'{item}'" for item in all_cd_aliases)
            all_wec_aliases_str = ','.join(f"'{item}'" for item in all_wec_aliases)

            logger.info(f"CD aliases:  {all_cd_aliases}")
            logger.info(f"WEC aliases: {all_wec_aliases}")

            layer_data = process_layer_data(
                processor, data_processor, layer,
                all_cd_aliases_str, all_wec_aliases_str,
                all_cd_aliases, all_wec_aliases, site)

            if layer_data is None:
                logger.warning(f"Layer {layer} @ {site}: no data in {config['days']}-day window — skipping to next layer")
                continue

            logger.info(f"Applying post-processing transforms for layer {layer}...")
            add_esc_zones(layer_data)
            rename_final_columns(layer_data)
            add_derived_columns(layer_data)
            layer_data = reorder_columns(layer_data)
            cleanup_and_sort(layer_data)

            if 'CD' not in layer_data.columns and len(config.get('cd_levels', [])) == 1:
                fallback_cd = config['cd_levels'][0]
                layer_data['CD'] = fallback_cd
                logger.warning(f"CD column missing in layer {layer}; defaulting CD to {fallback_cd}")

            if 'CD' in layer_data.columns:
                for cd_level in config['cd_levels']:
                    cd_chunk = layer_data[layer_data['CD'] == cd_level]
                    if not cd_chunk.empty:
                        temp_path = site_cd_temp_files[cd_level]
                        _append_layer_to_temp_csv(cd_chunk, temp_path)
                        logger.info(f"  Layer {layer} CD={cd_level}: appended {len(cd_chunk)} rows to temp CSV")
            else:
                logger.warning(f"CD column not found in layer {layer} data — skipping CD partition")

            del layer_data
            gc.collect()
            logger.info(f"Layer {layer} freed from memory after CD partition")

            # ── Update checkpoint ─────────────────────────────────────────
            completed_layers.append(layer)
            try:
                with open(checkpoint_path, 'w') as _cf:
                    json.dump({
                        'site':             site,
                        'days':             config['days'],
                        'cd_levels':        sorted(config['cd_levels']),
                        'layerRange':       config['layerRange'],
                        'incBM0':           config['incBM0'],
                        'completed_layers': completed_layers,
                        'updated':          datetime.now().isoformat()[:19],
                    }, _cf, indent=2)
                logger.info(f"Checkpoint updated: {len(completed_layers)}/{len(layerList)} layers complete for {site}")
            except Exception as _ce:
                logger.warning(f"Could not write checkpoint: {_ce}")

        logger.info("=" * 60)
        logger.info(f"Finalizing SDTT data for site {site} from per-layer temp CSVs")
        logger.info("=" * 60)
        finalize_site_data(processor, site_cd_temp_files, layerList, site, config)

        if os.path.exists(checkpoint_path):
            try:
                os.remove(checkpoint_path)
                logger.info(f"Checkpoint removed — {site} run complete")
            except Exception as _ce:
                logger.warning(f"Could not remove checkpoint: {_ce}")

        for cd_level, temp_path in site_cd_temp_files.items():
            if os.path.exists(temp_path):
                os.remove(temp_path)
                logger.info(f"Cleaned up temp file: {temp_path}")

    logger.info("=" * 80)
    logger.info("1280 SDTT Data Processing Script Completed Successfully")
    logger.info("=" * 80)


# ── DB query helper ────────────────────────────────────────────────────────────
def _read_sql_retry(sql: str, database_connection: str,
                    max_retries: int = 3, backoff_base: int = 30) -> pd.DataFrame:
    """Execute a SQL query via PyUber with automatic retry on transient errors."""
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
                f"DB query attempt {attempt}/{max_retries} failed: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            if attempt < max_retries:
                wait = backoff_base * attempt
                _logger.info(f"Waiting {wait}s before reconnect/retry...")
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


def _normalize_explicit_operations(values):
    """Return a de-duplicated list of string operation IDs from config values."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]

    normalized = []
    for value in values:
        op = str(value).strip()
        if op:
            normalized.append(op)

    # Keep stable order while removing duplicates.
    return list(dict.fromkeys(normalized))


# ── Layer-level processing ─────────────────────────────────────────────────────
def process_layer_data(processor, data_processor, layer, all_cd_aliases_str, all_wec_aliases_str,
                       all_cd_aliases, all_wec_aliases, site):
    """Process all chunks for a single layer and return the combined DataFrame."""
    logger = logging.getLogger(__name__)

    operation_mode = str(processor.config.get('operation_filtering_mode', 'alias-driven')).strip().lower()
    if operation_mode not in {'alias-driven', 'explicit-operations'}:
        logger.warning(f"Unknown operation_filtering_mode={operation_mode}; defaulting to alias-driven")
        operation_mode = 'alias-driven'

    explicit_ops = processor.config.get('explicit_operations', {})
    explicit_spc_ops = _normalize_explicit_operations(explicit_ops.get('spc'))
    explicit_wec_ops = _normalize_explicit_operations(explicit_ops.get('wec'))

    logger.info(
        f"Operation mode={operation_mode} "
        f"SPC ops={explicit_spc_ops if explicit_spc_ops else '[alias]'} "
        f"WEC ops={explicit_wec_ops if explicit_wec_ops else '[alias]'}"
    )

    if operation_mode == 'explicit-operations':
        if not explicit_spc_ops:
            logger.warning(f"Layer {layer} @ {site}: explicit SPC operation list is empty — skipping layer")
            return None

        canonical_spc_alias = all_cd_aliases[0] if all_cd_aliases else 'L_M1_FCCD'
        df_operalias = pd.DataFrame({
            'SITE': [site] * len(explicit_spc_ops),
            'DATA_SOURCE': ['EXPLICIT'] * len(explicit_spc_ops),
            'OPERATION': explicit_spc_ops,
            'GROUP_KEY': [pd.NA] * len(explicit_spc_ops),
            'ALIAS': [canonical_spc_alias] * len(explicit_spc_ops),
            'LAYER': [pd.NA] * len(explicit_spc_ops),
            'OPER_SHORT_DESC': ['explicit spc operation'] * len(explicit_spc_ops),
            'OPER_LONG_DESC': ['explicit spc operation'] * len(explicit_spc_ops),
        })
        mops = explicit_spc_ops
        logger.info(f"Using explicit SPC operations: {mops}")
    else:
        logger.info("Executing operalias query...")
        df_operalias = _read_sql_retry(
            QueryBuilder.operalias_query(all_cd_aliases_str, site), processor.database_connection)
        logger.info(f"Operation aliases retrieved: {len(df_operalias)} records")
        mops = df_operalias.OPERATION.to_list()

    if not mops:
        logger.warning(f"Layer {layer} @ {site}: no operation aliases found — skipping layer")
        return None
    mops_str = ','.join(f"'{item}'" for item in mops)
    processor.df_to_csv(df_operalias, f'cd_oper_alias_{site}_{layer}')
    logger.debug(f"MOPs: {mops}")

    logger.info("Executing SPC lot prefetch query...")
    df_spclot_prefetch = _read_sql_retry(
        QueryBuilder.spclot_prefetch_query(processor.config['days'], mops_str, site, layer),
        processor.database_connection)
    lots = df_spclot_prefetch.LOT.drop_duplicates().to_list()
    logger.info(f"SPC lots retrieved: {len(lots)} unique lots from {len(df_spclot_prefetch)} records")
    logger.info(f"Lot list: {lots}")
    processor.df_to_csv(df_spclot_prefetch, f'spc_lot_prefetch_{site}_{layer}')
    if not lots:
        logger.warning(f"Layer {layer} @ {site}: no lots found in {processor.config['days']}-day window — skipping layer")
        return None

    logger.info("Resolving WEC operations...")
    if operation_mode == 'explicit-operations':
        if not explicit_wec_ops:
            logger.warning(f"Layer {layer} @ {site}: explicit WEC operation list is empty — skipping layer")
            return None

        canonical_wec_alias = all_wec_aliases[0] if all_wec_aliases else 'WEC_FALLBACK'
        df_process_op_aliases = pd.DataFrame({
            'SITE': [site] * len(explicit_wec_ops),
            'DATA_SOURCE': ['EXPLICIT'] * len(explicit_wec_ops),
            'OPERATION': explicit_wec_ops,
            'GROUP_KEY': [pd.NA] * len(explicit_wec_ops),
            'ALIAS': [canonical_wec_alias] * len(explicit_wec_ops),
            'LAYER': [pd.NA] * len(explicit_wec_ops),
            'OPER_SHORT_DESC': ['explicit wec operation'] * len(explicit_wec_ops),
            'OPER_LONG_DESC': ['explicit wec operation'] * len(explicit_wec_ops),
        })
        logger.info(f"Using explicit WEC operations: {explicit_wec_ops}")
    else:
        if not all_wec_aliases_str.strip():
            logger.warning("No WEC aliases configured for this layer; skipping layer")
            return None

        df_process_op_aliases = _read_sql_retry(
            QueryBuilder.process_op_aliases_query(all_wec_aliases_str, site),
            processor.database_connection)

        if df_process_op_aliases.empty and processor.config.get('enable_wec_alias_relaxed_fallback', True):
            logger.warning("Strict WEC alias lookup returned 0 rows; retrying relaxed alias lookup")
            df_process_op_aliases = _read_sql_retry(
                QueryBuilder.process_op_aliases_query_relaxed(all_wec_aliases, site),
                processor.database_connection)

        if df_process_op_aliases.empty:
            overrides = processor.config.get('wec_operation_overrides', {})
            rows = []
            for alias in all_wec_aliases:
                for op in overrides.get(alias, []):
                    rows.append({
                        'SITE': site,
                        'DATA_SOURCE': 'OVERRIDE',
                        'OPERATION': str(op),
                        'GROUP_KEY': pd.NA,
                        'ALIAS': alias,
                        'LAYER': pd.NA,
                        'OPER_SHORT_DESC': 'manual override',
                        'OPER_LONG_DESC': 'manual override',
                    })

            if rows:
                logger.warning("WEC alias lookup returned 0 rows; using configured direct OPERATION overrides")
                df_process_op_aliases = pd.DataFrame(rows)

    if not df_process_op_aliases.empty:
        df_process_op_aliases = df_process_op_aliases.drop_duplicates(subset=['OPERATION']).reset_index(drop=True)

    logger.info(f"Process operation aliases retrieved: {len(df_process_op_aliases)} records")
    processor.df_to_csv(df_process_op_aliases, f'process_op_aliases_{site}_{layer}')
    wec_op_str = ','.join(f"'{item}'" for item in df_process_op_aliases.OPERATION.to_list())

    if not wec_op_str.strip():
        logger.warning("No WEC operations resolved for this layer; continuing with FCCD-only fallback")

    cd_alias_levels = processor.config.get('cd_alias_levels')

    if cd_alias_levels:
        all_cd_aliases = [alias for alias in all_cd_aliases if any(level in alias for level in cd_alias_levels)]
        all_cd_aliases_str = ','.join(f"'{item}'" for item in all_cd_aliases)
        logger.info(f"Restricted CD aliases to levels {cd_alias_levels}: {all_cd_aliases}")

    sdtt_chunks = []
    lot_chunks  = DataProcessor.chunk_list(lots, processor.config['nLots_chunk'])
    logger.info(f"Processing {len(lots)} lots in {len(lot_chunks)} chunks of {processor.config['nLots_chunk']}")

    for chunk_num, lot_chunk in enumerate(lot_chunks):
        logger.info(f"Processing chunk {chunk_num+1} of {len(lot_chunks)} ({len(lot_chunk)} lots)")
        lot_chunk_str = ','.join(f"'{item}'" for item in lot_chunk)

        chunk_data = process_chunk_data(
            processor, data_processor, lot_chunk_str, mops_str, wec_op_str,
            df_operalias, df_process_op_aliases,
            all_cd_aliases, all_wec_aliases, chunk_num, site, layer)

        if chunk_data is not None and not chunk_data.empty:
            sdtt_chunks.append(chunk_data)
            logger.info(f"Chunk {chunk_num+1} completed. Running total: {sum(len(c) for c in sdtt_chunks)} records")
        else:
            logger.warning(f"Chunk {chunk_num+1} of {len(lot_chunks)} returned no records — skipping")

    if not sdtt_chunks:
        logger.warning(f"Layer {layer} @ {site}: all chunks returned empty — skipping layer")
        return None
    logger.info(f"Combining {len(sdtt_chunks)} chunks for layer {layer}...")
    SDTT = pd.concat(sdtt_chunks, ignore_index=True)
    del sdtt_chunks
    gc.collect()

    logger.info(f"Layer {layer} processing complete. Total records: {len(SDTT)}")
    return finalize_layer_data(processor, SDTT, df_spclot_prefetch, layer)


def process_chunk_data(processor, data_processor, lot_chunk_str, mops_str, wec_op_str,
                       df_operalias, df_process_op_aliases,
                       all_cd_aliases, all_wec_aliases, chunk_num, site, layer):
    """Process data for a single chunk."""
    logger = logging.getLogger(__name__)
    db = processor.database_connection
    structure_list = processor.config.get('structures', [processor.config.get('structure', 'NEST')])
    use_structure_filter = processor.config.get('use_structure_sql_filter', True)

    logger.info("Executing lot run card query...")
    df_lot_run_card = _read_sql_retry(QueryBuilder.lot_run_card_query(lot_chunk_str, mops_str, layer), db)
    spcs = df_lot_run_card.SPCS_ID.drop_duplicates().to_list()
    if not spcs:
        logger.warning("No SPCS IDs found for this chunk; skipping chunk")
        return None
    spcs_id_str = ','.join(f"{item}" for item in spcs)
    logger.info(f"Lot run card data retrieved: {len(df_lot_run_card)} records, {len(spcs)} unique SPCS IDs")
    processor.df_to_csv(df_lot_run_card, f'lot_run_card_for_spcsid_{site}')

    include_raw_measurements = processor.config.get('include_raw_measurements', True)
    if include_raw_measurements:
        logger.info("Executing SPC measurements query...")
        df_measurements_raw = _read_sql_retry(
            QueryBuilder.spc_measurements_no_attr_split_query(spcs_id_str, structure_list, use_structure_filter), db)
        logger.info(f"SPC measurements retrieved: {len(df_measurements_raw)} raw records")
        processor.df_to_csv(df_measurements_raw, f'measurements_no_pivot_or_attr_split_{site}')
        measured_wafers = df_measurements_raw['WAFER_ID'].drop_duplicates().to_list()
        wafer_chunk_str = ','.join(f"'{item}'" for item in measured_wafers)
        logger.info(f"Extracted {len(measured_wafers)} unique wafers for WEC filtering")
    else:
        logger.info("RAW measurements disabled; wafer list will be derived from allstats")
        df_measurements_raw = None
        measured_wafers = []
        wafer_chunk_str = ''

    if include_raw_measurements and not measured_wafers:
        logger.warning("No measured wafers found for this chunk; skipping chunk")
        if df_measurements_raw is not None:
            del df_measurements_raw
        gc.collect()
        return None

    if include_raw_measurements:
        df_measurements_pivot = data_processor.process_measurements_data(df_measurements_raw, processor)
        processor.df_to_csv(df_measurements_pivot, f'spc_measurements_pivot_{site}')
        del df_measurements_raw
        gc.collect()
    else:
        df_measurements_pivot = None

    logger.info("Executing allstats query...")
    df_allstats_raw = _read_sql_retry(
        QueryBuilder.allstats_query(spcs_id_str, structure_list, use_structure_filter), db)
    logger.info(f"Allstats data retrieved: {len(df_allstats_raw)} raw records")
    processor.df_to_csv(df_allstats_raw, f'allstats_no_pivot_or_attr_split_{site}')

    df_allstats_pivot = data_processor.process_allstats_data(df_allstats_raw, processor)
    processor.df_to_csv(df_allstats_pivot, f'allstats_pivot_chunk{chunk_num+1}_{site}')
    del df_allstats_raw
    gc.collect()

    logger.info("Executing statistics query...")
    df_statistics_raw = _read_sql_retry(
        QueryBuilder.statistics_query(spcs_id_str, structure_list, use_structure_filter), db)
    logger.info(f"Statistics data retrieved: {len(df_statistics_raw)} raw records")
    processor.df_to_csv(df_statistics_raw, f'spc_statistics_{site}')

    df_statistics_pivot = data_processor.process_statistics_data(df_statistics_raw, processor)
    processor.df_to_csv(df_statistics_pivot, f'statistics_pivot_chunk{chunk_num+1}_{site}')
    del df_statistics_raw
    gc.collect()

    if not include_raw_measurements:
        measured_wafers = (
            df_allstats_pivot['WAFER_ID'].drop_duplicates().to_list()
            if 'WAFER_ID' in df_allstats_pivot.columns else []
        )
        wafer_chunk_str = ','.join(f"'{item}'" for item in measured_wafers)
        logger.info(f"Derived {len(measured_wafers)} wafers from allstats for WEC filtering")
        if not measured_wafers:
            logger.warning("No wafers available from allstats for this chunk; skipping chunk")
            return None

    if wec_op_str.strip():
        logger.info("Executing optimized WEC query with wafer filtering...")
        df_wec_subop = _read_sql_retry(
            QueryBuilder.wec_query_optimized(lot_chunk_str, wec_op_str, wafer_chunk_str, site), db)
        logger.info(f"WEC data retrieved (filtered by {len(measured_wafers)} wafers): {len(df_wec_subop)} raw records")
        processor.df_to_csv(df_wec_subop, f'wec_subop_{site}')

        desired_operations = ['Process-1', 'Chuck-1']
        if 'SUB_OPERATION' in df_wec_subop.columns:
            df_wec_subop = df_wec_subop[df_wec_subop['SUB_OPERATION'].isin(desired_operations)]
        logger.info(f"WEC data filtered to desired operations: {len(df_wec_subop)} records")

        df_wec = pd.merge(df_process_op_aliases, df_wec_subop, on='OPERATION', how='inner')
        df_wec.rename(columns={'ALIAS': 'WEC_ALIAS'}, inplace=True)
        logger.info(f"WEC data merged with aliases: {len(df_wec)} records")
        processor.df_to_csv(df_wec, f'wec_without_subops_{site}')
    else:
        fallback_alias = all_wec_aliases[0] if all_wec_aliases else 'WEC_FALLBACK'
        use_noop_fallback = processor.config.get('enable_wec_noop_fallback', True)

        if use_noop_fallback and wafer_chunk_str.strip():
            logger.warning("No WEC operations resolved; attempting no-op WEC fallback by lot+wafer")
            df_wec_subop = _read_sql_retry(
                QueryBuilder.wec_query_noop_fallback(lot_chunk_str, wafer_chunk_str, site), db)
            logger.info(f"No-op WEC fallback retrieved: {len(df_wec_subop)} raw records")

            if not df_wec_subop.empty:
                overrides = processor.config.get('wec_operation_overrides', {})
                allowed_ops = {
                    str(op)
                    for alias in all_wec_aliases
                    for op in overrides.get(alias, [])
                }
                if allowed_ops and 'OPERATION' in df_wec_subop.columns:
                    df_wec_subop = df_wec_subop[
                        df_wec_subop['OPERATION'].astype(str).isin(allowed_ops)
                    ]

                desired_operations = ['Process-1', 'Chuck-1']
                if 'SUB_OPERATION' in df_wec_subop.columns:
                    df_wec_subop = df_wec_subop[df_wec_subop['SUB_OPERATION'].isin(desired_operations)]

                if not df_wec_subop.empty and 'SUBENTITY_END_TIME' in df_wec_subop.columns:
                    df_wec_subop = df_wec_subop.sort_values(
                        ['WAFER_ID', 'SUBENTITY_END_TIME'], ascending=[True, False])

                df_wec_subop = df_wec_subop.drop_duplicates(subset=['WAFER_ID'], keep='first').copy()
                df_wec_subop['WEC_ALIAS'] = fallback_alias
                df_wec = df_wec_subop
                logger.info(f"No-op WEC fallback rows retained: {len(df_wec)} records")
                processor.df_to_csv(df_wec, f'wec_without_subops_{site}')
            else:
                df_wec = pd.DataFrame()
        else:
            df_wec = pd.DataFrame()

        if df_wec.empty:
            logger.warning("WEC no-op fallback unavailable; creating minimal FCCD-only WEC rows from measured wafers")
            df_wec = pd.DataFrame({
                'WEC_ALIAS': [fallback_alias] * len(measured_wafers),
                'WAFER_ID': measured_wafers,
            })
            logger.info(f"Fallback WEC rows created: {len(df_wec)} records")
            processor.df_to_csv(df_wec, f'wec_without_subops_{site}')

    logger.info("Joining all chunk data...")
    chunk_result = join_chunk_data(
        processor, df_allstats_pivot, df_statistics_pivot,
        df_measurements_pivot, df_wec, df_operalias,
        all_cd_aliases, all_wec_aliases)

    logger.info(f"Chunk data joined: {len(chunk_result)} final records")
    return chunk_result


def join_chunk_data(processor, df_allstats_pivot, df_statistics_pivot,
                    df_measurements_pivot, df_wec, df_operalias,
                    all_cd_aliases, all_wec_aliases):
    """Join all chunk DataFrames together."""
    logger = logging.getLogger(__name__)

    common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID', 'STRUCTURE']
    allstats_df   = df_allstats_pivot.rename(
        columns={col: 'ALLSTATS_' + col for col in df_allstats_pivot.columns if col not in common_cols})
    statistics_df = df_statistics_pivot.rename(
        columns={col: 'STATISTICS_' + col for col in df_statistics_pivot.columns if col not in common_cols})

    # Guard against accidental many-to-many joins by enforcing unique key rows.
    allstats_df = allstats_df.drop_duplicates(subset=common_cols)
    statistics_df = statistics_df.drop_duplicates(subset=common_cols)

    df_sj = pd.merge(allstats_df, statistics_df, on=common_cols, how='outer')
    processor.df_to_csv(df_sj, f'spc_join_{processor.site}')
    logger.debug(f"Allstats + Statistics join: {len(df_sj)} records")

    include_raw_measurements = processor.config.get('include_raw_measurements', True)
    if include_raw_measurements and df_measurements_pivot is not None and not df_measurements_pivot.empty:
        measurement_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID']
        df_measurements = df_measurements_pivot.rename(
            columns={col: 'MEASUREMENTS_' + col for col in df_measurements_pivot.columns if col not in common_cols})
        df_measurements = df_measurements.drop_duplicates(subset=measurement_cols)
        df_sj2 = pd.merge(df_sj, df_measurements, on=measurement_cols, how='outer')
        processor.df_to_csv(df_sj2, f'spc_join2_{processor.site}')
        logger.debug(f"+ Measurements join: {len(df_sj2)} records")
    else:
        df_sj2 = df_sj
        logger.info("RAW measurements disabled for this run; skipping measurements join")

    df_alias2op_spc = df_operalias[['OPERATION', 'ALIAS']].drop_duplicates().rename(
        columns={'OPERATION': 'ALLSTATS_OPERATION', 'ALIAS': 'ALLSTATS_ALIAS'})
    df_sj3 = pd.merge(df_sj2, df_alias2op_spc, on=['ALLSTATS_OPERATION'], how='inner')
    df_sj3.rename(columns={'ALLSTATS_ALIAS': 'SPC_ALIAS'}, inplace=True)
    logger.debug(f"+ Operation aliases join: {len(df_sj3)} records")

    my_dict = dict(zip(all_cd_aliases, all_wec_aliases))
    df_sj3['WEC_ALIAS'] = df_sj3['SPC_ALIAS'].map(my_dict)
    processor.df_to_csv(df_sj3, f'spc_join3_{processor.site}')

    common_cols = ['WEC_ALIAS', 'WAFER_ID']
    df_wec_merge = df_wec.rename(
        columns={col: 'WEC_' + col for col in df_wec.columns if col not in common_cols})
    df_wec_merge = df_wec_merge.drop_duplicates(subset=common_cols)
    df_sdtt = pd.merge(df_sj3, df_wec_merge, on=common_cols, how='inner')
    processor.df_to_csv(df_sdtt, f'sdtt_chunk_{processor.site}')
    logger.debug(f"Final join result: {len(df_sdtt)} records")

    return df_sdtt


def finalize_layer_data(processor, SDTT, df_spclot_prefetch, layer):
    """Finalize processing for a single layer."""
    logger = logging.getLogger(__name__)
    logger.info(f"Finalizing layer {layer} data...")

    spclot_prefetch_cols_to_keep = ['LOT7', 'LOT_TYPE', 'PRODUCT DEVREVSTEP', 'DATA_COLLECTION_TIME']
    df_spclot_prefetch.columns = [
        'SPC_' + col if col not in spclot_prefetch_cols_to_keep else col
        for col in df_spclot_prefetch.columns]

    SDTT['SPC_LOT']       = SDTT['ALLSTATS_LOT'].copy()
    SDTT['SPC_OPERATION'] = SDTT['ALLSTATS_OPERATION'].copy()

    SDTT_fin = pd.merge(SDTT, df_spclot_prefetch, on=['SPC_LOT', 'SPC_OPERATION'], how='inner')
    logger.info(f"Layer {layer} finalized: {len(SDTT_fin)} records")

    _layer_csv = os.path.join(processor.folder_path, f'SDTT_1280_M{layer}_{processor.site}.csv')
    SDTT_fin.to_csv(_layer_csv, index=False)
    logger.debug(f"Layer checkpoint written: {_layer_csv}")
    return SDTT_fin


def finalize_site_data(processor, site_cd_temp_files, layerList, site, config):
    """Read per-CD temp CSVs and write final output CSVs.

    No APC join for 1280.
    """
    logger = logging.getLogger(__name__)

    for cd_level in config['cd_levels']:
        temp_path = site_cd_temp_files.get(cd_level)
        if not temp_path or not os.path.exists(temp_path):
            logger.warning(f"No temp CSV found for CD level {cd_level} in site {site} — skipping")
            continue

        logger.info(f"Loading accumulated temp CSV for {cd_level}_{site}...")
        cd_data = pd.read_csv(temp_path, low_memory=False)
        logger.info(f"Found {len(cd_data)} records for {cd_level} in site {site}")

        if cd_data.empty:
            logger.warning(f"Temp CSV for {cd_level}_{site} is empty — skipping")
            continue

        csv_name = f"{config['main_csv_base_name']}_{cd_level}_{site}"
        logger.info(f"Saving data for {cd_level}_{site}...")
        save_cd_level_data(processor, cd_data, csv_name, cd_level, site)

        del cd_data
        gc.collect()


def save_cd_level_data(processor, cd_data_new, csv_name, cd_level, site):
    """Save CD level data, merging with any existing CSV and removing redundant columns."""
    logger = logging.getLogger(__name__)
    include_raw_measurements = processor.config.get('include_raw_measurements', True)

    # Constrain persisted rows to aliases implied by the active layerRange for this run.
    layer_cfg = processor.config.get('layerRange', [])
    if isinstance(layer_cfg, list) and layer_cfg:
        start_layer = layer_cfg[0]
        end_layer = layer_cfg[1] if len(layer_cfg) > 1 else layer_cfg[0]
        active_layers = list(range(start_layer, end_layer + 1)) if start_layer <= end_layer else [start_layer]
    else:
        active_layers = []

    alias_prefix = processor.config.get('tech_alias_nums', {}).get(processor.config.get('tech', ''), '')
    allowed_aliases = {f"L_{alias_prefix}M{ly}_{cd_level}" for ly in active_layers} if active_layers else set()

    if allowed_aliases and 'SPC_ALIAS' in cd_data_new.columns:
        before = len(cd_data_new)
        cd_data_new = cd_data_new[cd_data_new['SPC_ALIAS'].astype(str).isin(allowed_aliases)].copy()
        removed = before - len(cd_data_new)
        if removed:
            logger.info(f"Removed {removed} rows outside active alias scope: {sorted(allowed_aliases)}")

    # Columns to remove per CD level — will be tuned after first full run (Step 8 in plan)
    columns_to_remove = {
        'FCCD': [
            'ALLSTATS_SCANNER_GROUP',
            'MEASUREMENTS_WAFER_RADIUS_1_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_2_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_3_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_4_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_5_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_6_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_7_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_8_ZONE',
            'STATISTICS_WAFER_MEAN_CENTERLINE',
            'STATISTICS_WAFER_MEAN_CHART_ID',
            'STATISTICS_WAFER_MEAN_CHART_POINT_SEQ',
            'STATISTICS_WAFER_MEAN_CHART_TYPE',
            'STATISTICS_WAFER_MEAN_CORRECTED_FLAG',
            'STATISTICS_WAFER_MEAN_INCONTROL_FLAG',
            'STATISTICS_WAFER_MEAN_INDISPOSITION_FLAG',
            'STATISTICS_WAFER_MEAN_LCL',
            'STATISTICS_WAFER_MEAN_LDL',
            'STATISTICS_WAFER_MEAN_STANDARD_FLAG',
            'STATISTICS_WAFER_MEAN_TARGET',
            'STATISTICS_WAFER_MEAN_UCL',
            'STATISTICS_WAFER_MEAN_UDL',
            'STATISTICS_WAFER_MEAN_VALID_FLAG',
            'STATISTICS_WAFER_MEAN_VALUE',
            'STATISTICS_WAFER_MEAN_VIOLATED_RULE_NOTATION',
            'STATISTICS_WAFER_SIGMA_CENTERLINE',
            'STATISTICS_WAFER_SIGMA_CHART_ID',
            'STATISTICS_WAFER_SIGMA_CHART_POINT_SEQ',
            'STATISTICS_WAFER_SIGMA_CHART_TYPE',
            'STATISTICS_WAFER_SIGMA_CORRECTED_FLAG',
            'STATISTICS_WAFER_SIGMA_INCONTROL_FLAG',
            'STATISTICS_WAFER_SIGMA_INDISPOSITION_FLAG',
            'STATISTICS_WAFER_SIGMA_LCL',
            'STATISTICS_WAFER_SIGMA_STANDARD_FLAG',
            'STATISTICS_WAFER_SIGMA_UCL',
            'STATISTICS_WAFER_SIGMA_UDL',
            'STATISTICS_WAFER_SIGMA_VALID_FLAG',
            'STATISTICS_WAFER_SIGMA_VALUE',
            'STATISTICS_WAFER_SIGMA_VIOLATED_RULE_NOTATION',
        ],
        'DCCD': [
            'ALLSTATS_MEAN_DTT_VALUE',
            'ALLSTATS_MEAN_TARGET_VALUE',
            'ALLSTATS_SIGMA_DTT_VALUE',
            'ALLSTATS_SIGMA_TARGET_VALUE',
            'MEASUREMENTS_WAFER_RADIUS_1_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_2_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_3_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_4_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_5_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_6_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_7_ZONE',
            'MEASUREMENTS_WAFER_RADIUS_8_ZONE',
            'STATISTICS_MEAN_DTT_CENTERLINE',
            'STATISTICS_MEAN_DTT_CHART_ID',
            'STATISTICS_MEAN_DTT_CHART_POINT_SEQ',
            'STATISTICS_MEAN_DTT_CHART_TYPE',
            'STATISTICS_MEAN_DTT_CORRECTED_FLAG',
            'STATISTICS_MEAN_DTT_INCONTROL_FLAG',
            'STATISTICS_MEAN_DTT_INDISPOSITION_FLAG',
            'STATISTICS_MEAN_DTT_LCL',
            'STATISTICS_MEAN_DTT_LDL',
            'STATISTICS_MEAN_DTT_STANDARD_FLAG',
            'STATISTICS_MEAN_DTT_UCL',
            'STATISTICS_MEAN_DTT_UDL',
            'STATISTICS_MEAN_DTT_VALID_FLAG',
            'STATISTICS_MEAN_DTT_VALUE',
            'STATISTICS_MEAN_DTT_VIOLATED_RULE_NOTATION',
            'STATISTICS_SIGMA_DTT_CENTERLINE',
            'STATISTICS_SIGMA_DTT_CHART_ID',
            'STATISTICS_SIGMA_DTT_CHART_POINT_SEQ',
            'STATISTICS_SIGMA_DTT_CHART_TYPE',
            'STATISTICS_SIGMA_DTT_CORRECTED_FLAG',
            'STATISTICS_SIGMA_DTT_INCONTROL_FLAG',
            'STATISTICS_SIGMA_DTT_INDISPOSITION_FLAG',
            'STATISTICS_SIGMA_DTT_LCL',
            'STATISTICS_SIGMA_DTT_LDL',
            'STATISTICS_SIGMA_DTT_STANDARD_FLAG',
            'STATISTICS_SIGMA_DTT_UCL',
            'STATISTICS_SIGMA_DTT_UDL',
            'STATISTICS_SIGMA_DTT_VALID_FLAG',
            'STATISTICS_SIGMA_DTT_VALUE',
        ],
        'HCCD': [
            'ALLSTATS_SCANNER_GROUP',
            'STATISTICS_WAFER_MEAN_CENTERLINE',
            'STATISTICS_WAFER_MEAN_CHART_ID',
            'STATISTICS_WAFER_MEAN_CHART_POINT_SEQ',
            'STATISTICS_WAFER_MEAN_CHART_TYPE',
            'STATISTICS_WAFER_MEAN_CORRECTED_FLAG',
            'STATISTICS_WAFER_MEAN_INCONTROL_FLAG',
            'STATISTICS_WAFER_MEAN_INDISPOSITION_FLAG',
            'STATISTICS_WAFER_MEAN_LCL',
            'STATISTICS_WAFER_MEAN_LDL',
            'STATISTICS_WAFER_MEAN_STANDARD_FLAG',
            'STATISTICS_WAFER_MEAN_TARGET',
            'STATISTICS_WAFER_MEAN_UCL',
            'STATISTICS_WAFER_MEAN_UDL',
            'STATISTICS_WAFER_MEAN_VALID_FLAG',
            'STATISTICS_WAFER_MEAN_VALUE',
            'STATISTICS_WAFER_MEAN_VIOLATED_RULE_NOTATION',
            'STATISTICS_WAFER_SIGMA_CENTERLINE',
            'STATISTICS_WAFER_SIGMA_CHART_ID',
            'STATISTICS_WAFER_SIGMA_CHART_POINT_SEQ',
            'STATISTICS_WAFER_SIGMA_CHART_TYPE',
            'STATISTICS_WAFER_SIGMA_CORRECTED_FLAG',
            'STATISTICS_WAFER_SIGMA_INCONTROL_FLAG',
            'STATISTICS_WAFER_SIGMA_INDISPOSITION_FLAG',
            'STATISTICS_WAFER_SIGMA_LCL',
            'STATISTICS_WAFER_SIGMA_STANDARD_FLAG',
            'STATISTICS_WAFER_SIGMA_UCL',
            'STATISTICS_WAFER_SIGMA_UDL',
            'STATISTICS_WAFER_SIGMA_VALID_FLAG',
            'STATISTICS_WAFER_SIGMA_VALUE',
            'STATISTICS_WAFER_SIGMA_VIOLATED_RULE_NOTATION',
        ],
    }

    if cd_level in columns_to_remove:
        cols_to_remove = columns_to_remove[cd_level]
        existing_cols_to_remove = [col for col in cols_to_remove if col in cd_data_new.columns]
        if existing_cols_to_remove:
            cd_data_new = cd_data_new.drop(columns=existing_cols_to_remove)
            logger.info(f"Removed {len(existing_cols_to_remove)} unnecessary columns from {cd_level}_{site} data")

    if not include_raw_measurements:
        measurement_cols_new = [c for c in cd_data_new.columns if c.startswith('MEASUREMENTS_')]
        if measurement_cols_new:
            cd_data_new = cd_data_new.drop(columns=measurement_cols_new)
            logger.info(f"RAW OFF: dropped {len(measurement_cols_new)} MEASUREMENTS_* columns from new data")

    cd_data_old = processor.main_csv_to_df(csv_name)

    if not cd_data_old.empty:
        logger.info(f"Found existing data for {cd_level}_{site}: {len(cd_data_old)} records")

        if allowed_aliases and 'SPC_ALIAS' in cd_data_old.columns:
            before_old = len(cd_data_old)
            cd_data_old = cd_data_old[cd_data_old['SPC_ALIAS'].astype(str).isin(allowed_aliases)].copy()
            removed_old = before_old - len(cd_data_old)
            if removed_old:
                logger.info(f"Removed {removed_old} existing rows outside active alias scope: {sorted(allowed_aliases)}")

        if cd_level in columns_to_remove:
            cols_to_remove = columns_to_remove[cd_level]
            existing_old = [col for col in cols_to_remove if col in cd_data_old.columns]
            if existing_old:
                cd_data_old = cd_data_old.drop(columns=existing_old)

        if not include_raw_measurements:
            measurement_cols_old = [c for c in cd_data_old.columns if c.startswith('MEASUREMENTS_')]
            if measurement_cols_old:
                cd_data_old = cd_data_old.drop(columns=measurement_cols_old)
                logger.info(f"RAW OFF: dropped {len(measurement_cols_old)} MEASUREMENTS_* columns from existing data")

        dup_subset = ['WAFER_ID', 'TEST_NAME']
        if 'STRUCTURE' in cd_data_new.columns and 'STRUCTURE' in cd_data_old.columns:
            dup_subset.append('STRUCTURE')

        dup_keys = cd_data_new[dup_subset].drop_duplicates()
        cd_data_old_filtered = (
            cd_data_old
            .merge(dup_keys, on=dup_subset, how='left', indicator=True)
            .query('_merge == "left_only"')
            .drop(columns='_merge')
            .reset_index(drop=True)
        )
        duplicates_removed = len(cd_data_old) - len(cd_data_old_filtered)
        logger.info(f"Removed {duplicates_removed} duplicate records from existing {cd_level}_{site} data")

        if cd_data_old_filtered.empty:
            cd_data_final = cd_data_new.copy()
            logger.info(f"Combined {cd_level}_{site}: 0 existing + {len(cd_data_new)} new = {len(cd_data_final)} total")
        else:
            cd_data_final = pd.concat([cd_data_old_filtered, cd_data_new], ignore_index=True)
            logger.info(f"Combined {cd_level}_{site}: {len(cd_data_old_filtered)} existing + "
                        f"{len(cd_data_new)} new = {len(cd_data_final)} total")
    else:
        cd_data_final = cd_data_new
        logger.info(f"No existing data for {cd_level}_{site}. Using new data only: {len(cd_data_final)} records")

    cd_data_final.reset_index(drop=True, inplace=True)

    # MT1 operational guardrail: FCCD MT1 rows must report canonical WEC operation 269250.
    if 'SPC_ALIAS' in cd_data_final.columns and 'WEC_OPERATION' in cd_data_final.columns:
        cd_data_final['WEC_OPERATION'] = cd_data_final['WEC_OPERATION'].astype('string')
        mt1_mask = cd_data_final['SPC_ALIAS'].astype(str).eq('L_M1_FCCD')
        if mt1_mask.any():
            cd_data_final.loc[mt1_mask, 'WEC_OPERATION'] = '269250'

    if 'DATA_COLLECTION_TIME' in cd_data_final.columns:
        cd_data_final['DATA_COLLECTION_TIME'] = pd.to_datetime(
            cd_data_final['DATA_COLLECTION_TIME'],
            format='mixed', dayfirst=False, errors='coerce')
        cd_data_final.sort_values(by='DATA_COLLECTION_TIME', ascending=False, inplace=True)
        cd_data_final.reset_index(drop=True, inplace=True)
        logger.info(f"Sorted {cd_level}_{site} final table by DATA_COLLECTION_TIME (descending)")

    processor.main_df_to_csv(cd_data_final, csv_name, 1)

    today = date.today()
    date_string = today.strftime("%Y-%m-%d")
    final_message = (f"CSV created/updated for {cd_level}_{site}: "
                     f"{date_string} {csv_name}.csv ({len(cd_data_final)} records)")
    print(final_message)
    logger.info(final_message)


def _append_layer_to_temp_csv(df: pd.DataFrame, path: str) -> None:
    """Append *df* to a per-CD temp CSV, guaranteeing column alignment across layers."""
    logger = logging.getLogger(__name__)

    if not os.path.exists(path):
        df.to_csv(path, index=False)
        return

    existing_cols = pd.read_csv(path, nrows=0).columns.tolist()
    new_cols      = df.columns.tolist()
    union_cols    = existing_cols + [c for c in new_cols if c not in existing_cols]

    if len(union_cols) > len(existing_cols):
        extra = [c for c in union_cols if c not in existing_cols]
        logger.debug(f"Temp CSV expanded by {len(extra)} columns: {extra} — rewriting")
        existing_df = pd.read_csv(path, low_memory=False)
        existing_df = existing_df.reindex(columns=union_cols)
        existing_df.to_csv(path, index=False)

    df.reindex(columns=union_cols).to_csv(path, mode='a', header=False, index=False)


def add_esc_zones(SDTT):
    """ESC zone derivation is disabled for KARC explicit-operation adaptation."""
    logger = logging.getLogger(__name__)
    logger.debug("ESC zone derivation skipped")


def add_derived_columns(SDTT):
    """Add PROD_MOP and PROD_MOP_PILOT derived columns.

    Must be called AFTER rename_final_columns() so 'PRODUCT' exists.
    """
    logger = logging.getLogger(__name__)

    op_col    = 'ALLSTATS_OPERATION'
    prod_col  = 'PRODUCT'
    pilot_col = 'ALLSTATS_PILOT_NAME'

    if op_col in SDTT.columns and prod_col in SDTT.columns:
        SDTT['PROD_MOP'] = SDTT[prod_col].astype(str) + '_' + SDTT[op_col].astype(str)
        logger.debug("Added PROD_MOP column")
    else:
        missing = [c for c in [op_col, prod_col] if c not in SDTT.columns]
        logger.warning(f"PROD_MOP skipped — missing columns: {missing}")

    if op_col in SDTT.columns and prod_col in SDTT.columns and pilot_col in SDTT.columns:
        SDTT['PROD_MOP_PILOT'] = (
            SDTT[prod_col].astype(str) + '_'
            + SDTT[op_col].astype(str) + '_'
            + SDTT[pilot_col].astype(str)
        )
        logger.debug("Added PROD_MOP_PILOT column")
    else:
        missing = [c for c in [op_col, prod_col, pilot_col] if c not in SDTT.columns]
        logger.warning(f"PROD_MOP_PILOT skipped — missing columns: {missing}")


def rename_final_columns(SDTT):
    """Rename columns to final output names."""
    logger = logging.getLogger(__name__)

    column_renames = {
        'ALLSTATS_PRODUCT_GROUP':   'PRODUCT_GROUP',
        'PRODUCT DEVREVSTEP':       'PRODUCT',
        'ALLSTATS_STRUCTURE':       'STRUCTURE',
        'ALLSTATS_ROUTE_TYPE':      'ROUTE_TYPE',
        'ALLSTATS_IS_POR':          'IS_POR',
        'MEASUREMENTS_WAFER_RECIPE':'SPC_RECIPE',
        'WEC_WAFER_SHORT':          'WID',
        'WEC_DB_BATCH_ID':          'DB_BATCH_ID',
        'ALLSTATS_PRIMARY_ENTITY':  'PRIMARY_ENTITY',
        'ALLSTATS_ANALYTICAL_ENTITY':'ANALYTICAL_ENTITY',
        'WEC_SUBENTITY':            'SUBENTITY',
        'MEASUREMENTS_CD':          'CD',
        'MEASUREMENTS_LAYER':       'LAYER',
    }

    renamed_count = 0
    for old_name, new_name in column_renames.items():
        if old_name in SDTT.columns:
            SDTT.rename(columns={old_name: new_name}, inplace=True)
            renamed_count += 1

    if 'WEC_SUBENTITY_END_TIME' in SDTT.columns:
        SDTT['SUBENTITY_END_TIME'] = SDTT['WEC_SUBENTITY_END_TIME'].copy()
    if 'SPC_ROUTE' in SDTT.columns:
        SDTT['ROUTE'] = SDTT['SPC_ROUTE'].copy()

    logger.debug(f"Renamed {renamed_count} columns")


def reorder_columns(SDTT):
    """Reorder columns: priority list first, remaining sorted alphabetically."""
    logger = logging.getLogger(__name__)

    columns_to_move = [
        'DATA_COLLECTION_TIME',
        'SPC_LOT', 'WID', 'IS_POR', 'WAFER_ID', 'TEST_NAME', 'PRODUCT',
        'SUBENTITY', 'SUBENTITY_END_TIME',
        'WEC_OPERATION', 'WEC_RECIPE', 'WEC_LAYER',
        'ALLSTATS_MEAN_DTT_VALUE', 'ALLSTATS_MEAN_TARGET_VALUE', 'ALLSTATS_WAFER_MEAN_VALUE',
        'ALLSTATS_SIGMA_DTT_VALUE', 'ALLSTATS_SIGMA_TARGET_VALUE', 'ALLSTATS_WAFER_SIGMA_VALUE',
        'ROUTE', 'SPC_OPERATION', 'PROD_MOP', 'PROD_MOP_PILOT',
        # Identity / lineage
        'ANALYTICAL_ENTITY', 'PRIMARY_ENTITY',
        'SPC_RECIPE', 'SPC_PILOT_NAME', 'SPC_ALIAS',
        'WEC_LOT', 'WEC_ALIAS', 'ROUTE_TYPE', 'LAYER', 'CD',
        'STRUCTURE', 'PRODUCT_GROUP', 'LOT7', 'LOT_TYPE', 'SPCS_ID', 'DB_BATCH_ID',
    ]

    def get_sort_keys(item, split_char='-'):
        parts = item.split(split_char)
        if len(parts) >= 3:
            return (0, parts[0], parts[1], parts[2])
        elif len(parts) >= 2:
            return (1, parts[0], parts[1], "")
        else:
            return (2, item, "", "")

    current_columns = SDTT.columns.tolist()
    remaining_columns = [col for col in current_columns if col not in columns_to_move]
    sorted_remaining = sorted(remaining_columns, key=get_sort_keys)
    existing_columns_to_move = [col for col in columns_to_move if col in current_columns]
    new_column_order = existing_columns_to_move + sorted_remaining
    SDTT = SDTT.reindex(columns=new_column_order)

    logger.debug(f"Reordered columns: {len(existing_columns_to_move)} priority + {len(sorted_remaining)} remaining")
    return SDTT


def cleanup_and_sort(SDTT):
    """Remove Unnamed columns and parse datetime columns.

    Sorting is deferred to save_cd_level_data() where it covers all layers + existing rows.
    """
    logger = logging.getLogger(__name__)

    unnamed_cols = SDTT.columns[SDTT.columns.str.contains('Unnamed', case=False)]
    if len(unnamed_cols) > 0:
        SDTT.drop(columns=unnamed_cols, inplace=True)
        logger.debug(f"Removed {len(unnamed_cols)} unnamed columns")

    if 'SUBENTITY_END_TIME' in SDTT.columns:
        SDTT['SUBENTITY_END_TIME'] = pd.to_datetime(
            SDTT['SUBENTITY_END_TIME'], format='mixed', dayfirst=False, errors='coerce')
    if 'DATA_COLLECTION_TIME' in SDTT.columns:
        SDTT['DATA_COLLECTION_TIME'] = pd.to_datetime(
            SDTT['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')

    SDTT.reset_index(drop=True, inplace=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='1280 KARC adaptation (MT1/MT2 FCCD only)')
    parser.add_argument('--days', type=int, default=CONFIG['days'], help='Lookback window in days')
    parser.add_argument(
        '--operation-mode',
        choices=['alias-driven', 'explicit-operations'],
        default=CONFIG.get('operation_filtering_mode', 'alias-driven'),
        help='Operation filtering mode override for this run')
    parser.add_argument(
        '--spc-ops',
        type=str,
        default=None,
        help='Comma-separated SPC operation list override (for explicit mode)')
    parser.add_argument(
        '--wec-ops',
        type=str,
        default=None,
        help='Comma-separated WEC operation list override (for explicit mode)')
    args = parser.parse_args()

    def _parse_cli_ops(raw_value):
        if raw_value is None:
            return None
        return [item.strip() for item in raw_value.split(',') if item.strip()]

    CONFIG['days'] = args.days
    CONFIG['operation_filtering_mode'] = args.operation_mode

    cli_spc_ops = _parse_cli_ops(args.spc_ops)
    cli_wec_ops = _parse_cli_ops(args.wec_ops)
    if cli_spc_ops is not None:
        CONFIG.setdefault('explicit_operations', {})['spc'] = cli_spc_ops
    if cli_wec_ops is not None:
        CONFIG.setdefault('explicit_operations', {})['wec'] = cli_wec_ops

    _ok = False
    try:
        main()
        _ok = True
    finally:
        # Work around intermittent Python.Runtime shutdown crashes after successful completion.
        if _ok:
            os._exit(0)
