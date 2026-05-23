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
CONFIG = {
    'ceid': 'AMEct',
    'sites': ['D1V'],

    'database_connections': {
        'D1V': 'D1D_PROD_XEUS_LOCAL',
    },

    'tech': "1280",
    'structure': "NEST",
    'tech_alias_nums': {"1278": "8", "1280": ""},
    # Narrow range for initial test — override via PIPELINE_CONFIG for full runs
    'layerRange': [6, 7],
    'incBM0': 0,
    'days': 7,
    'nLots_chunk': 25,
    # debug_writes is authoritative in PIPELINE_CONFIG; True here for standalone runs.
    'debug_writes': False,
    'folder_path': '\\\\orshfs.intel.com\\ORAnalysis$\\1276_MAODATA\\Config\\etch\\AME\\tbatson\\sDTT\\sDTT_rev01\\debug\\1280 QUERY FILES\\',
    'main_csv_path': '\\\\orshfs.intel.com\\ORAnalysis$\\1276_MAODATA\\Config\\etch\\AME\\tbatson\\sDTT\\sDTT_rev01\\debug\\',
    'main_csv_base_name': '1280sDTT',
    'cd_levels': ['HCCD', 'DCCD', 'FCCD'],
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
    def spclot_prefetch_query(days, mops_str, site):
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
          ON  l.LOTOPERKEY=h.LOTOPERKEY
          AND l.MONITOR_TYPE='WIP MONITOR'
        INNER JOIN P_SPC_SESSION s
          ON  s.SPCS_ID=l.SPCS_ID
          AND s.LATEST_FLAG='Y'
        WHERE h.LOT IN ({spc_lot_str})
          AND h.OPERATION IN ({mops_str})
        """

    @staticmethod
    def spc_measurements_no_attr_split_query(spcs_id_str):
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
          AND  ((m.MEASUREMENT_SET_NAME='CD.FCCD_MEASUREMENTS.80' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (m.MEASUREMENT_SET_NAME='CD.DCCD_MEASUREMENTS.80' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\'))
        """

    @staticmethod
    def allstats_query(spcs_id_str):
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
          AND  ((m.MEASUREMENT_SET_NAME='CD.FCCD_ALLSTATS.80' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (m.MEASUREMENT_SET_NAME='CD.DCCD_ALLSTATS.80' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\'))
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
          AND UPPER(a1.OPER_GROUP_NAME) IN ({all_wec_aliases_str})
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
        AND c.WAFER IN ({wafer_chunk_str})"""

    @staticmethod
    def wec_query_sed_only(spc_lot_str, sed_op_str, wafer_chunk_str, site):
        """Query SED operations for SCANNER and RETICLE."""
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
    def wec_query_etch_only(spc_lot_str, etch_op_str, wafer_chunk_str, site):
        """Query HM_ETCH and MAIN_ETCH operations for SUBENTITY (AME_ETCH / GTO_ETCH)."""
        return f"""
        SELECT DISTINCT c.WAFER "WAFER_ID"
        ,c.ENTITY "SCANNER"
        ,c.SUBENTITY
        ,c.OPERATION
        FROM F_WAFERCHAMBERHIST c
        WHERE c.OPERATION IN ({etch_op_str})
        AND c.WAFER IN ({wafer_chunk_str})
        """

    @staticmethod
    def statistics_query(spcs_id_str):
        # Uses string_contains() UDF (1280 XEUS DB) instead of LIKE chains.
        # DENSE_RANK partition includes c.CHART_PARAMETER for 1280 granularity.
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
          AND  ((cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS.80' AND string_contains(c.CHART_ATTRIBUTES,'STRUCTURE=NEST','AND',';')=1) OR
                        (cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS_ST.80' AND string_contains(c.CHART_ATTRIBUTES,'STRUCTURE=NEST','AND',';')=1) OR
                        (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS.80' AND string_contains(c.CHART_ATTRIBUTES,'STRUCTURE=NEST','AND',';')=1) OR
                        (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS_ST.80' AND string_contains(c.CHART_ATTRIBUTES,'STRUCTURE=NEST','AND',';')=1))
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

        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('ATTRIBUTES', axis=1)
        processor.df_to_csv(df_attr_split, 'measurements_attr_split_no_pivot')

        df_attr_split['WAFER_RADIUS'] = np.sqrt(
            df_attr_split['WAFER_X']**2 + df_attr_split['WAFER_Y']**2)

        df_attr_split['WAFER_RECIPE'] = df_attr_split['WAFER_RECIPE'].fillna('MISSING')

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

        common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID']
        if df_raw.empty:
            self.logger.warning("Allstats query returned 0 rows; returning empty frame with join keys")
            return pd.DataFrame(columns=common_cols)

        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop(['ATTRIBUTES', 'PARAMETERS', 'MEASUREMENT_ID'], axis=1)

        # 1280 PILOT_NAME guard — column is often absent in allstats results
        if 'PILOT_NAME' in df_attr_split.columns:
            df_attr_split['PILOT_NAME'] = df_attr_split['PILOT_NAME'].fillna('MISSING')
        else:
            df_attr_split['PILOT_NAME'] = 'MISSING'

        pivot_index = ['WAFER_ID', 'SPCS_ID', 'DYNWAFER', 'IS_POR']
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

        common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID']
        if df_raw.empty:
            self.logger.warning("Statistics query returned 0 rows; returning empty frame with join keys")
            return pd.DataFrame(columns=common_cols)

        parsed_data = df_raw['CHART_ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('CHART_ATTRIBUTES', axis=1)

        pivot_index = ['WAFER_ID', 'SPCS_ID']
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

            # ── 1280 alias generation — no BM0, tech1 = "" ────────────────
            # HCCD: E_M{layer}_HM_ETCH  → WEC alias
            # DCCD: L_M{layer}_SED      → WEC alias
            # FCCD: E_V{layer-1}_MAIN_ETCH → WEC alias (via etch at layer-1)
            # MOP:  L_M{layer}_HCCD / L_M{layer}_DCCD / L_M{layer}_FCCD
            wec = {
                "HCCD": "E_{}M{}_HM_ETCH",
                "DCCD": "L_{}M{}_SED",
                "FCCD": "E_{}V{}_MAIN_ETCH",
            }
            wec_layers = {"HCCD": [layer], "DCCD": [layer], "FCCD": [layer - 1]}
            cd_layers  = {"HCCD": [layer], "DCCD": [layer], "FCCD": [layer]}

            all_wec_aliases = []
            all_cd_aliases  = []
            mop_temp = "L_{}M{}_{}"

            for cd in wec:
                for wec_layer in wec_layers[cd]:
                    all_wec_aliases.append(wec[cd].format(tech1, wec_layer))
                for cd_layer in cd_layers[cd]:
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


# ── Layer-level processing ─────────────────────────────────────────────────────
def process_layer_data(processor, data_processor, layer, all_cd_aliases_str, all_wec_aliases_str,
                       all_cd_aliases, all_wec_aliases, site):
    """Process all chunks for a single layer and return the combined DataFrame."""
    logger = logging.getLogger(__name__)

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
        QueryBuilder.spclot_prefetch_query(processor.config['days'], mops_str, site),
        processor.database_connection)
    lots = df_spclot_prefetch.LOT.drop_duplicates().to_list()
    logger.info(f"SPC lots retrieved: {len(lots)} unique lots from {len(df_spclot_prefetch)} records")
    logger.info(f"Lot list: {lots}")
    processor.df_to_csv(df_spclot_prefetch, f'spc_lot_prefetch_{site}_{layer}')
    if not lots:
        logger.warning(f"Layer {layer} @ {site}: no lots found in {processor.config['days']}-day window — skipping layer")
        return None

    logger.info("Executing process operation aliases query (WEC)...")
    df_process_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(all_wec_aliases_str, site),
        processor.database_connection)
    logger.info(f"Process operation aliases retrieved: {len(df_process_op_aliases)} records")
    processor.df_to_csv(df_process_op_aliases, f'process_op_aliases_{site}_{layer}')
    wec_op_str = ','.join(f"'{item}'" for item in df_process_op_aliases.OPERATION.to_list())

    # Split aliases for separate SED / ETCH WEC queries
    sed_aliases       = [alias for alias in all_wec_aliases if 'SED' in alias]
    hm_etch_aliases   = [alias for alias in all_wec_aliases if 'HM_ETCH' in alias]
    main_etch_aliases = [alias for alias in all_wec_aliases if 'MAIN_ETCH' in alias]

    sed_aliases_str  = ','.join(f"'{item}'" for item in sed_aliases)
    etch_aliases     = hm_etch_aliases + main_etch_aliases
    etch_aliases_str = ','.join(f"'{item}'" for item in etch_aliases)

    logger.info(f"SED aliases:        {sed_aliases}")
    logger.info(f"HM_ETCH aliases:    {hm_etch_aliases}")
    logger.info(f"MAIN_ETCH aliases:  {main_etch_aliases}")

    logger.info("Executing SED operation aliases query...")
    df_sed_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(sed_aliases_str, site),
        processor.database_connection)
    logger.info(f"SED operation aliases retrieved: {len(df_sed_op_aliases)} records")
    processor.df_to_csv(df_sed_op_aliases, f'sed_op_aliases_{site}_{layer}')
    sed_op_str = ','.join(f"'{item}'" for item in df_sed_op_aliases.OPERATION.to_list())

    logger.info("Executing ETCH operation aliases query...")
    df_etch_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(etch_aliases_str, site),
        processor.database_connection)
    logger.info(f"ETCH operation aliases retrieved: {len(df_etch_op_aliases)} records")
    processor.df_to_csv(df_etch_op_aliases, f'etch_op_aliases_{site}_{layer}')
    etch_op_str = ','.join(f"'{item}'" for item in df_etch_op_aliases.OPERATION.to_list())

    df_minimal_op_aliases = pd.concat([df_sed_op_aliases, df_etch_op_aliases], ignore_index=True)
    processor.df_to_csv(df_minimal_op_aliases, f'minimal_op_aliases_combined_{site}_{layer}')

    sdtt_chunks = []
    lot_chunks  = DataProcessor.chunk_list(lots, processor.config['nLots_chunk'])
    logger.info(f"Processing {len(lots)} lots in {len(lot_chunks)} chunks of {processor.config['nLots_chunk']}")

    for chunk_num, lot_chunk in enumerate(lot_chunks):
        logger.info(f"Processing chunk {chunk_num+1} of {len(lot_chunks)} ({len(lot_chunk)} lots)")
        lot_chunk_str = ','.join(f"'{item}'" for item in lot_chunk)

        chunk_data = process_chunk_data(
            processor, data_processor, lot_chunk_str, mops_str, wec_op_str,
            sed_op_str, etch_op_str, df_operalias, df_process_op_aliases,
            df_minimal_op_aliases, all_cd_aliases, all_wec_aliases, chunk_num, site)

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
                       sed_op_str, etch_op_str, df_operalias, df_process_op_aliases,
                       df_minimal_op_aliases, all_cd_aliases, all_wec_aliases, chunk_num, site):
    """Process data for a single chunk."""
    logger = logging.getLogger(__name__)
    db = processor.database_connection

    logger.info("Executing lot run card query...")
    df_lot_run_card = _read_sql_retry(QueryBuilder.lot_run_card_query(lot_chunk_str, mops_str), db)
    spcs = df_lot_run_card.SPCS_ID.drop_duplicates().to_list()
    spcs_id_str = ','.join(f"{item}" for item in spcs)
    logger.info(f"Lot run card data retrieved: {len(df_lot_run_card)} records, {len(spcs)} unique SPCS IDs")
    processor.df_to_csv(df_lot_run_card, f'lot_run_card_for_spcsid_{site}')

    logger.info("Executing SPC measurements query...")
    df_measurements_raw = _read_sql_retry(
        QueryBuilder.spc_measurements_no_attr_split_query(spcs_id_str), db)
    logger.info(f"SPC measurements retrieved: {len(df_measurements_raw)} raw records")
    processor.df_to_csv(df_measurements_raw, f'measurements_no_pivot_or_attr_split_{site}')

    measured_wafers = df_measurements_raw['WAFER_ID'].drop_duplicates().to_list()
    wafer_chunk_str = ','.join(f"'{item}'" for item in measured_wafers)
    logger.info(f"Extracted {len(measured_wafers)} unique wafers for WEC filtering")

    df_measurements_pivot = data_processor.process_measurements_data(df_measurements_raw, processor)
    processor.df_to_csv(df_measurements_pivot, f'spc_measurements_pivot_{site}')
    del df_measurements_raw
    gc.collect()

    logger.info("Executing allstats query...")
    df_allstats_raw = _read_sql_retry(QueryBuilder.allstats_query(spcs_id_str), db)
    logger.info(f"Allstats data retrieved: {len(df_allstats_raw)} raw records")
    processor.df_to_csv(df_allstats_raw, f'allstats_no_pivot_or_attr_split_{site}')

    df_allstats_pivot = data_processor.process_allstats_data(df_allstats_raw, processor)
    processor.df_to_csv(df_allstats_pivot, f'allstats_pivot_chunk{chunk_num+1}_{site}')
    del df_allstats_raw
    gc.collect()

    logger.info("Executing statistics query...")
    df_statistics_raw = _read_sql_retry(QueryBuilder.statistics_query(spcs_id_str), db)
    logger.info(f"Statistics data retrieved: {len(df_statistics_raw)} raw records")
    processor.df_to_csv(df_statistics_raw, f'spc_statistics_{site}')

    df_statistics_pivot = data_processor.process_statistics_data(df_statistics_raw, processor)
    processor.df_to_csv(df_statistics_pivot, f'statistics_pivot_chunk{chunk_num+1}_{site}')
    del df_statistics_raw
    gc.collect()

    logger.info("Executing optimized WEC query with wafer filtering...")
    df_wec_subop = _read_sql_retry(
        QueryBuilder.wec_query_optimized(lot_chunk_str, wec_op_str, wafer_chunk_str, site), db)
    logger.info(f"WEC data retrieved (filtered by {len(measured_wafers)} wafers): {len(df_wec_subop)} raw records")
    processor.df_to_csv(df_wec_subop, f'wec_subop_{site}')

    logger.info("Executing SED query for SCANNER and RETICLE...")
    df_wec_sed = _read_sql_retry(
        QueryBuilder.wec_query_sed_only(lot_chunk_str, sed_op_str, wafer_chunk_str, site), db)
    logger.info(f"SED WEC data retrieved: {len(df_wec_sed)} records")

    logger.info("Executing ETCH query for SUBENTITY...")
    df_wec_etch = _read_sql_retry(
        QueryBuilder.wec_query_etch_only(lot_chunk_str, etch_op_str, wafer_chunk_str, site), db)
    logger.info(f"ETCH WEC data retrieved: {len(df_wec_etch)} records")

    processor.df_to_csv(df_wec_sed,  f'wec_sed_{site}')
    processor.df_to_csv(df_wec_etch, f'wec_etch_{site}')

    df_wec_minimal_processed = process_minimal_wec_data_separate(
        df_wec_sed, df_wec_etch, df_minimal_op_aliases)

    logger.info("Joining minimal WEC data with main WEC data...")
    df_wec_subop_enhanced = pd.merge(df_wec_subop, df_wec_minimal_processed,
                                     on='WAFER_ID', how='left')
    logger.info(f"Enhanced WEC data: {len(df_wec_subop_enhanced)} records")
    processor.df_to_csv(df_wec_subop_enhanced, f'wec_subop_enhanced_{site}')

    desired_operations = ['Process-1', 'Chuck-1']
    df_wec_subop_enhanced = df_wec_subop_enhanced[
        df_wec_subop_enhanced['SUB_OPERATION'].isin(desired_operations)]
    logger.info(f"WEC data filtered to desired operations: {len(df_wec_subop_enhanced)} records")

    df_wec = pd.merge(df_process_op_aliases, df_wec_subop_enhanced, on='OPERATION', how='inner')
    df_wec.rename(columns={'ALIAS': 'WEC_ALIAS'}, inplace=True)
    logger.info(f"WEC data merged with aliases: {len(df_wec)} records")
    processor.df_to_csv(df_wec, f'wec_without_subops_{site}')

    logger.info("Joining all chunk data...")
    chunk_result = join_chunk_data(
        processor, df_allstats_pivot, df_statistics_pivot,
        df_measurements_pivot, df_wec, df_operalias,
        all_cd_aliases, all_wec_aliases)

    logger.info(f"Chunk data joined: {len(chunk_result)} final records")
    return chunk_result


def process_minimal_wec_data_separate(df_wec_sed, df_wec_etch, df_minimal_op_aliases):
    """Process separate SED and ETCH WEC DataFrames into per-wafer summary."""
    logger = logging.getLogger(__name__)

    sed_processed = df_wec_sed.groupby('WAFER_ID').agg({
        'SCANNER': 'first',
        'RETICLE': 'first',
    }).reset_index()
    sed_processed.columns = ['WAFER_ID', 'SCANNER_MINIMAL', 'RETICLE_MINIMAL']

    if not df_wec_etch.empty:
        df_etch_with_aliases = pd.merge(
            df_wec_etch,
            df_minimal_op_aliases[['OPERATION', 'ALIAS']],
            on='OPERATION', how='left')

        hm_etch_data   = df_etch_with_aliases[df_etch_with_aliases['ALIAS'].str.contains('HM_ETCH',   na=False)]
        main_etch_data = df_etch_with_aliases[df_etch_with_aliases['ALIAS'].str.contains('MAIN_ETCH', na=False)]

        ame_etch = hm_etch_data.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        ame_etch.columns = ['WAFER_ID', 'AME_ETCH']

        gto_etch = main_etch_data.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        gto_etch.columns = ['WAFER_ID', 'GTO_ETCH']

        etch_processed = pd.merge(ame_etch, gto_etch, on='WAFER_ID', how='outer')
    else:
        etch_processed = pd.DataFrame(columns=['WAFER_ID', 'AME_ETCH', 'GTO_ETCH'])

    result = pd.merge(sed_processed, etch_processed, on='WAFER_ID', how='outer')

    logger.info(f"Processed separate WEC data: {len(result)} wafers")
    logger.info(f"SCANNER_MINIMAL non-null: {result['SCANNER_MINIMAL'].notna().sum()}")
    logger.info(f"RETICLE_MINIMAL non-null: {result['RETICLE_MINIMAL'].notna().sum()}")
    logger.info(f"AME_ETCH non-null: {result['AME_ETCH'].notna().sum()}")
    logger.info(f"GTO_ETCH non-null: {result['GTO_ETCH'].notna().sum()}")
    return result


def join_chunk_data(processor, df_allstats_pivot, df_statistics_pivot,
                    df_measurements_pivot, df_wec, df_operalias,
                    all_cd_aliases, all_wec_aliases):
    """Join all chunk DataFrames together."""
    logger = logging.getLogger(__name__)

    common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID']
    allstats_df   = df_allstats_pivot.rename(
        columns={col: 'ALLSTATS_' + col for col in df_allstats_pivot.columns if col not in common_cols})
    statistics_df = df_statistics_pivot.rename(
        columns={col: 'STATISTICS_' + col for col in df_statistics_pivot.columns if col not in common_cols})

    df_sj = pd.merge(allstats_df, statistics_df, on=common_cols, how='outer')
    processor.df_to_csv(df_sj, f'spc_join_{processor.site}')
    logger.debug(f"Allstats + Statistics join: {len(df_sj)} records")

    df_measurements = df_measurements_pivot.rename(
        columns={col: 'MEASUREMENTS_' + col for col in df_measurements_pivot.columns if col not in common_cols})
    df_sj2 = pd.merge(df_sj, df_measurements, on=common_cols, how='outer')
    processor.df_to_csv(df_sj2, f'spc_join2_{processor.site}')
    logger.debug(f"+ Measurements join: {len(df_sj2)} records")

    df_alias2op_spc = df_operalias[['OPERATION', 'ALIAS']].rename(
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

    cd_data_old = processor.main_csv_to_df(csv_name)

    if not cd_data_old.empty:
        logger.info(f"Found existing data for {cd_level}_{site}: {len(cd_data_old)} records")

        if cd_level in columns_to_remove:
            cols_to_remove = columns_to_remove[cd_level]
            existing_old = [col for col in cols_to_remove if col in cd_data_old.columns]
            if existing_old:
                cd_data_old = cd_data_old.drop(columns=existing_old)

        dup_keys = cd_data_new[['WAFER_ID', 'TEST_NAME']].drop_duplicates()
        cd_data_old_filtered = (
            cd_data_old
            .merge(dup_keys, on=['WAFER_ID', 'TEST_NAME'], how='left', indicator=True)
            .query('_merge == "left_only"')
            .drop(columns='_merge')
            .reset_index(drop=True)
        )
        duplicates_removed = len(cd_data_old) - len(cd_data_old_filtered)
        logger.info(f"Removed {duplicates_removed} duplicate records from existing {cd_level}_{site} data")

        cd_data_final = pd.concat([cd_data_old_filtered, cd_data_new], ignore_index=True)
        logger.info(f"Combined {cd_level}_{site}: {len(cd_data_old_filtered)} existing + "
                    f"{len(cd_data_new)} new = {len(cd_data_final)} total")
    else:
        cd_data_final = cd_data_new
        logger.info(f"No existing data for {cd_level}_{site}. Using new data only: {len(cd_data_final)} records")

    cd_data_final.reset_index(drop=True, inplace=True)

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
    """Add ESC zone columns based on WAFER_RADIUS and AME entity prefix."""
    logger = logging.getLogger(__name__)
    zone_columns_added = 0
    for col_idx, col_name in enumerate(SDTT.columns):
        if 'WAFER_RADIUS' in col_name:
            new_col_name = f'{col_name}_ZONE'
            conditions = [
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] < 38),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 38)   & (SDTT[col_name] < 108),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 108)  & (SDTT[col_name] < 128.5),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 128.5),
            ]
            choices = ['I', 'MI', 'MO', 'O']
            SDTT[new_col_name] = np.select(conditions, choices, default='')
            zone_columns_added += 1
    logger.debug(f"Added {zone_columns_added} ESC zone columns")


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
        'WEC_RETICLE_MINIMAL':      'RETICLE',
        'WEC_SCANNER_MINIMAL':      'SCANNER',
        'WEC_AME_ETCH':             'AME_ETCH',
        'WEC_GTO_ETCH':             'GTO_ETCH',
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
        'SCANNER', 'RETICLE', 'AME_ETCH', 'GTO_ETCH',
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
    main()
