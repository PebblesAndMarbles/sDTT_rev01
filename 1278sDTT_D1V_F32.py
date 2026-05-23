# -*- coding: utf-8 -*-
"""
SDTT Data Processing Script - Refactored with Logging
Created on Fri Aug 15 13:56:47 2025
@author: tbatson
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

# Configuration - Modified to support multiple sites
CONFIG = {
    'ceid': 'AMEct',
    #'sites': ['F32', 'D1V'],  # List of sites to process
    'sites': ['D1V'],  # List of sites to process    

    'database_connections': {
        'F32': 'F32_PROD_XEUS_GAJT',
        'D1V': 'D1D_PROD_XEUS_LOCAL'
    },

    'tech': "1278",
    'structure': "NEST",
    'tech_alias_nums': {"1278": "8", "1280": ""},
    # layerRange and incBM0 are authoritative in PIPELINE_CONFIG when run via
    # the pipeline orchestrator; kept here as fallback defaults for standalone runs.
    'layerRange': [5, 5],
    'incBM0': 1,
    'days': 120,
    'nLots_chunk': 25,
    # debug_writes is authoritative in PIPELINE_CONFIG; True here for standalone runs.
    'debug_writes': False,
    'folder_path': '\\\\orshfs.intel.com\\ORAnalysis$\\1276_MAODATA\\Config\\etch\\AME\\tbatson\\sDTT\\sDTT_rev01\\debug\\1278 QUERY FILES\\',
    'main_csv_path': '\\\\orshfs.intel.com\\ORAnalysis$\\1276_MAODATA\\Config\\etch\\AME\\tbatson\\sDTT\\sDTT_rev01\\debug\\',
    'main_csv_base_name': '1278sDTT',  # Base name without site
    'cd_levels': ['HCCD', 'DCCD', 'FCCD'],  # CD levels to process
    #'cd_levels': ['HCCD'],  # CD levels to process
    'suppress_sqlalchemy_warnings': True,
    'log_level': 'INFO'  # DEBUG, INFO, WARNING, ERROR, CRITICAL
}

# Set pandas display options
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)
pd.set_option('display.max_rows', None)

def setup_logging(log_level='INFO'):
    """Configure logging for the application.

    If the root logger already has handlers (e.g. this script was imported by
    the pipeline orchestrator which configured logging itself), skip
    re-initialisation entirely.  Creating a FileHandler as a side-effect of
    a no-op basicConfig call opens a file handle on the network drive before
    it is attached to any logger, which can interfere with subsequent pandas
    CSV writes on UNC paths.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        # Logging already configured by caller — just return the module logger.
        return logging.getLogger(__name__)

    # ── Standalone execution: configure from scratch ──────────────────────
    # Create logs directory if it doesn't exist
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # Create log filename with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_filename = os.path.join(log_dir, f'sdtt_processing_{timestamp}.log')
    
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler()  # Also log to console
        ]
    )

    # Silence third-party loggers that echo SQL at INFO level
    for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
                   'sqlalchemy', 'PyUber', 'pyuber'):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Log file: {log_filename}")
    return logger

def setup_warning_filters(suppress_sqlalchemy=True):
    """Configure warning filters for cleaner console output"""
    logger = logging.getLogger(__name__)
    
    if suppress_sqlalchemy:
        # Filter out SQLAlchemy warnings specifically
        warnings.filterwarnings('ignore', 
                              message='.*pandas only supports SQLAlchemy connectable.*',
                              category=UserWarning)
        logger.info("SQLAlchemy connection warnings are being suppressed for cleaner output.")
        logger.info("Set CONFIG['suppress_sqlalchemy_warnings'] = False to show these warnings.")

class SDTTProcessor:
    def __init__(self, config, site):
        self.config = config
        self.site = site
        self.database_connection = config['database_connections'][site]
        self.logger = logging.getLogger(f"sDTT_{site}_ENGINE")
        # Ensure output directories exist (important when called from pipeline
        # which may redirect main_csv_path to a newly created folder).
        os.makedirs(config['folder_path'], exist_ok=True)
        os.makedirs(config['main_csv_path'], exist_ok=True)
        self.setup_paths()
        
    def setup_paths(self):
        """Initialize file paths and CSV handling functions"""
        self.folder_path = self.config['folder_path']
        self.main_csv_path = self.config['main_csv_path']
        self.logger.debug(f"Paths initialized - Working: {self.folder_path}, Main: {self.main_csv_path}")
        
    def main_df_to_csv(self, df, name, no_index=None, show=None):
        """Save DataFrame to main CSV location using atomic replace for safety."""
        csvwritefile = os.path.join(self.main_csv_path, f'{name}.csv')
        tmpfile = csvwritefile + '.tmp'
        try:
            if no_index == 1:
                df.to_csv(tmpfile, index=False)
            else:
                df.to_csv(tmpfile)
            os.replace(tmpfile, csvwritefile)
        except KeyboardInterrupt:
            # Preserve the previous CSV if write is interrupted mid-stream.
            if os.path.exists(tmpfile):
                try:
                    os.remove(tmpfile)
                except Exception:
                    pass
            self.logger.error(f"CSV write interrupted for {csvwritefile}; existing file left unchanged")
            raise
        self.logger.debug(f"Saved DataFrame to main CSV: {csvwritefile} ({len(df)} rows)")
        if show == 1:
            os.startfile(csvwritefile)
            
    def main_csv_to_df(self, name):
        """Load DataFrame from main CSV location with existence check"""
        csvreadfile = os.path.join(self.main_csv_path, f'{name}.csv')
        if os.path.exists(csvreadfile):
            df = pd.read_csv(csvreadfile, low_memory=False)
            self.logger.info(f"Loaded existing CSV: {csvreadfile} ({len(df)} rows)")
            return df
        else:
            self.logger.info(f"CSV file {csvreadfile} does not exist. Creating new dataset.")
            return pd.DataFrame()  # Return empty DataFrame if file doesn't exist
            
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
        """Load DataFrame from working folder"""
        csvreadfile = os.path.join(self.folder_path, f'{name}.csv')
        df = pd.read_csv(csvreadfile, low_memory=False)
        self.logger.debug(f"Loaded DataFrame from working folder: {name}.csv ({len(df)} rows)")
        return df

class QueryBuilder:
    """Centralized query management"""
    
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
        WHERE a1.DATA_SOURCE IN ('D1_P1278')
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
        AND l.LOT_PROCESS='1278'
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
          AND  ((m.MEASUREMENT_SET_NAME='CD.FCCD_MEASUREMENTS.78' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (m.MEASUREMENT_SET_NAME='CD.DCCD_MEASUREMENTS.78' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\'))
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
          AND  ((m.MEASUREMENT_SET_NAME='CD.FCCD_ALLSTATS.78' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (m.MEASUREMENT_SET_NAME='CD.DCCD_ALLSTATS.78' AND a.ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\'))
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
        WHERE a1.DATA_SOURCE IN ('D1_P1278')
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
        """Query for SED operations to get SCANNER and RETICLE"""
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
        """Query for HM_ETCH and MAIN_ETCH operations to get SUBENTITY"""
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
        -- REMOVED: ,SUBSTR(c.CHART_PARAMETER,2,LENGTH(c.CHART_PARAMETER)-2) "PARAMETERS"
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
        ,DENSE_RANK() OVER (PARTITION BY l.LOT, l.OPERATION, cp.MEASUREMENT_SET_NAME, cp.CHART_TYPE ORDER BY cp.DATA_COLLECTION_TIME DESC) "PASS_ORDER"
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
        AND  ((cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=MEAN\_DTT;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=SIGMA\_DTT;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=WAFER\_MEAN;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.FCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=WAFER\_SIGMA;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=MEAN\_DTT;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=SIGMA\_DTT;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=WAFER\_MEAN;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\') OR
            (cp.MEASUREMENT_SET_NAME='CD.DCCD_STATISTICS.78' AND c.CHART_ATTRIBUTES LIKE '%;CD\_TERMS=WAFER\_SIGMA;%' ESCAPE '\\' AND c.CHART_ATTRIBUTES LIKE '%;STRUCTURE=NEST;%' ESCAPE '\\'))
        AND NVL(cp.WAFER,w.WAFER) IS NOT NULL
        ) x
        WHERE x.PASS_ORDER=1
        """

class DataProcessor:
    """Handle data processing operations"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    @staticmethod
    def parse_attributes(attr_string):
        """Parse the attributes string into dictionary"""
        attributes = {}
        pairs = attr_string.strip(';').split(';')
        for pair in pairs:
            if '=' in pair:
                variable, value = pair.split('=', 1)
                attributes[variable] = value
        return attributes

    @staticmethod
    def chunk_list(spcs, chunk_length):
        """Separate elements of a list into sublists of specified length"""
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
        """Generate layer list from range"""
        if b is None:
            return [a]
        else:
            if a > b:
                return []
            return list(range(a, b + 1))

    def process_measurements_data(self, df_raw, processor):
        """Process SPC measurements data with pivot"""
        self.logger.info(f"Processing measurements data: {len(df_raw)} raw records")
        
        # Parse attributes
        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('ATTRIBUTES', axis=1)
        processor.df_to_csv(df_attr_split, 'measurements_attr_split_no_pivot')
        
        # Calculate wafer radius
        df_attr_split['WAFER_RADIUS'] = np.sqrt(df_attr_split['WAFER_X']**2 + df_attr_split['WAFER_Y']**2)
        
        # Fill missing wafer recipe
        df_attr_split['WAFER_RECIPE'] = df_attr_split['WAFER_RECIPE'].fillna('MISSING')
        
        # Pivot
        df_pivot = df_attr_split.pivot_table(
            index=['SPCS_ID', 'MEASUREMENT_SET_NAME', 'TEST_NAME', 'WAFER_ID', 'WAFER_RECIPE'],
            columns='MEASURE_INDEX',
            values=['WAFER_RADIUS', 'VALUE'],
            aggfunc='first')
        
        df_pivot.columns = ['_'.join(col).strip() for col in df_pivot.columns.values]
        df_pivot.reset_index(inplace=True)
        
        # Create CD and LAYER columns
        df_pivot['CD'] = df_pivot['TEST_NAME'].str.slice(start=4, stop=8)
        condition = df_pivot['TEST_NAME'].str.endswith('H')
        df_pivot.loc[condition, 'CD'] = 'H' + df_pivot.loc[condition, 'CD'].str[1:]
        df_pivot['LAYER'] = df_pivot['TEST_NAME'].str.slice(start=9, stop=12)
        
        self.logger.info(f"Measurements data processed: {len(df_pivot)} pivoted records")
        return df_pivot

    def process_allstats_data(self, df_raw, processor):
        """Process allstats data with pivot"""
        self.logger.info(f"Processing allstats data: {len(df_raw)} raw records")
        
        # Parse attributes
        parsed_data = df_raw['ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop(['ATTRIBUTES', 'PARAMETERS', 'MEASUREMENT_ID'], axis=1)
        df_attr_split['PILOT_NAME'] = df_attr_split['PILOT_NAME'].fillna('MISSING')
        
        # Pivot
        pivot_index = ['WAFER_ID', 'SPCS_ID', 'DYNWAFER', 'IS_POR']
        pivot_column = ['CD_TERMS']
        pivot_values = ['VALUE']
        
        df_pivot_nomerge = df_attr_split.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first')
        
        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(col).strip() for col in df_pivot_nomerge.columns.values]
        df_pivot_nomerge.reset_index(inplace=True)
        
        # Merge with non-pivot columns
        df_merge_cols = df_attr_split.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='inner')
        
        self.logger.info(f"Allstats data processed: {len(df_pivot)} pivoted records")
        return df_pivot

    def process_statistics_data(self, df_raw, processor):
        """Process statistics data with pivot - PARAMETERS column removed from query"""
        self.logger.info(f"Processing statistics data: {len(df_raw)} raw records")
        
        # Parse attributes (PARAMETERS no longer exists in the data)
        parsed_data = df_raw['CHART_ATTRIBUTES'].apply(DataProcessor.parse_attributes)
        new_columns_df = pd.DataFrame(parsed_data.tolist())
        df_attr_split = pd.concat([df_raw, new_columns_df], axis=1)
        df_attr_split = df_attr_split.drop('CHART_ATTRIBUTES', axis=1)
        
        # Pivot
        pivot_index = ['WAFER_ID', 'SPCS_ID']
        pivot_column = ['CD_TERMS']
        pivot_values = ['VALUE', 'CENTERLINE', 'TARGET', 'LCL', 'UCL', 'LDL', 'UDL', 'LSL', 'USL',
                    'VALID_FLAG', 'STANDARD_FLAG', 'CORRECTED_FLAG', 'INCONTROL_FLAG', 
                    'INDISPOSITION_FLAG', 'VIOLATED_RULE_NOTATION', 'CHART_ID', 'CHART_TYPE', 
                    'CHART_POINT_SEQ']
        
        df_pivot_nomerge = df_attr_split.pivot_table(
            index=pivot_index, columns=pivot_column, values=pivot_values, aggfunc='first')
        
        df_pivot_nomerge.columns = df_pivot_nomerge.columns.swaplevel(0, 1)
        df_pivot_nomerge.columns = ['_'.join(col).strip() for col in df_pivot_nomerge.columns.values]
        df_pivot_nomerge.reset_index(inplace=True)
        
        # Merge with non-pivot columns (no PARAMETERS to exclude)
        df_merge_cols = df_attr_split.drop(columns=pivot_column + pivot_values).drop_duplicates()
        df_pivot = pd.merge(df_pivot_nomerge, df_merge_cols, on=pivot_index, how='outer')
        
        self.logger.info(f"Statistics data processed: {len(df_pivot)} pivoted records")
        return df_pivot

def main(config=None):
    """Entry point for the SPC/WEC data pipeline.

    Parameters
    ----------
    config : dict, optional
        Configuration dictionary.  When *None* (default) the module-level
        ``CONFIG`` dict is used, so the script continues to work unchanged
        when run directly (``python 1278sDTT_D1V_F32.py``).
    """
    if config is None:
        config = CONFIG

    # Setup warning filters based on config
    setup_warning_filters(config['suppress_sqlalchemy_warnings'])

    # Generate layer list
    tech1 = config['tech_alias_nums'][config['tech']]
    layerList = DataProcessor.get_layerList(config['layerRange'][0], 
                                          config['layerRange'][1] if len(config['layerRange']) > 1 else None)
    if config['incBM0'] == 1:
        layerList.append("BM0")

    for site in config['sites']:
        logger = logging.getLogger(f"sDTT_{site}_ENGINE")
        logger.info("="*80)
        logger.info("SDTT Data Processing Script Started")
        logger.info("="*80)
        logger.info(f"Configuration: {config}")
        logger.info(f"Processing layers: {layerList}")
        logger.info(f"Tech alias: {tech1}, Days: {config['days']}, Chunk size: {config['nLots_chunk']}")
        logger.info(f"{'='*80}")
        logger.info(f"Processing Site: {site}")
        logger.info(f"Database Connection: {config['database_connections'][site]}")
        logger.info(f"{'='*80}")

        # Initialize processor for this site
        processor = SDTTProcessor(config, site)
        data_processor = DataProcessor()

        # ── Per-layer CD temp CSV approach ────────────────────────────────
        # Instead of accumulating ALL layer DataFrames in a dict (which holds
        # the full dataset in RAM simultaneously for all layers), we:
        #   1. Apply post-processing transforms per layer
        #   2. Split by CD level and APPEND each CD slice to a per-CD temp CSV
        #   3. Delete the layer DataFrame immediately to free RAM
        # Peak memory = one layer's data, not all layers combined.
        site_cd_temp_files = {}

        # ── Checkpoint / resume support ───────────────────────────────────────
        resume = config.get('resume', False)
        checkpoint_path = os.path.join(processor.folder_path, f'sdtt_checkpoint_{site}.json')
        completed_layers = []

        if resume and os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, 'r') as _cf:
                    _ckpt = json.load(_cf)
                # Validate that checkpoint matches the current run parameters
                if (    _ckpt.get('days')        == config['days']
                    and sorted(_ckpt.get('cd_levels', [])) == sorted(config['cd_levels'])
                    and _ckpt.get('layerRange')  == config['layerRange']
                    and _ckpt.get('incBM0')      == config['incBM0']):
                    completed_layers = _ckpt.get('completed_layers', [])
                    logger.info(f"Resuming {site}: {len(completed_layers)} layers already done: {completed_layers}")
                else:
                    logger.warning(
                        f"Checkpoint config mismatch for {site} — starting fresh (ignoring checkpoint).\n"
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
                                     f'sdtt_site_{site}_{cd_level}_temp.csv')
            site_cd_temp_files[cd_level] = temp_path
            if resume and completed_layers:
                # Keep existing temp CSVs — they contain data from completed layers
                if os.path.exists(temp_path):
                    logger.info(f"Keeping existing temp CSV for resume: {os.path.basename(temp_path)}")
            else:
                # Normal start: remove any stale temp file from a previous run
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        
        for index0, layer in enumerate(layerList):
            logger.info(f"{'='*60}")
            logger.info(f"Processing Site {site} - Layer {index0+1} of {len(layerList)}: {layer}")
            logger.info(f"{'='*60}")

            # ── Resume: skip layers already recorded in checkpoint ─────────
            if layer in completed_layers:
                logger.info(f"  [RESUME] Layer {layer} already completed — skipping")
                continue

            # Generate aliases based on layer
            if layer == "BM0":
                wec = {"HCCD": "E_8BM0_HM_ETCH", "DCCD": "L_8BM0_SED", "FCCD": "E_8BCN_ILD_ETCH"}
                mop = {"HCCD": "L_8BM0_HCCD", "DCCD": "L_8BM0_DCCD", "FCCD": "L_8BM0_FCCD"}
                wec_layers = {"HCCD": ["BM0"], "DCCD": ["BM0"], "FCCD": ["BCN"]}
                cd_layers = {"HCCD": ["BM0"], "DCCD": ["BM0"], "FCCD": ["BCN"]}
            else:
                wec = {"HCCD": "E_{}M{}_HM_ETCH", "DCCD": "L_{}M{}_SED", "FCCD": "E_{}V{}_MAIN_ETCH"}
                wec_layers = {"HCCD": [layer], "DCCD": [layer], "FCCD": [layer-1]}
                cd_layers = {"HCCD": [layer], "DCCD": [layer], "FCCD": [layer]}
            
            # Build alias strings
            all_wec_aliases = []
            all_cd_aliases = []
            mop_temp = "L_{}M{}_{}"
            
            for cd in wec:
                for wec_layer in wec_layers[cd]:
                    if layer == "BM0":
                        all_wec_aliases.append(wec[cd])
                    else:
                        all_wec_aliases.append(wec[cd].format(tech1, wec_layer))
                for cd_layer in cd_layers[cd]:
                    if layer == "BM0":
                        all_cd_aliases.append(mop[cd])
                    else:
                        all_cd_aliases.append(mop_temp.format(tech1, cd_layer, cd))
            
            all_cd_aliases_str = ','.join(f"'{item}'" for item in all_cd_aliases)
            all_wec_aliases_str = ','.join(f"'{item}'" for item in all_wec_aliases)
            
            logger.info(f"CD aliases: {all_cd_aliases}")
            logger.info(f"WEC aliases: {all_wec_aliases}")
            
            # Process layer data
            layer_data = process_layer_data(processor, data_processor, layer, all_cd_aliases_str, 
                                          all_wec_aliases_str, all_cd_aliases, all_wec_aliases, site)
            
            # ── Apply post-processing transforms per layer ─────────────────
            # (Previously done in finalize_site_data on the full combined DF;
            # applying per-layer is equivalent — no transform reads cross-layer.)
            logger.info(f"Applying post-processing transforms for layer {layer}...")
            add_esc_zones(layer_data)
            rename_final_columns(layer_data)
            add_derived_columns(layer_data)   # PROD_MOP, PROD_MOP_PILOT (needs PRODUCT, so after rename)
            layer_data = reorder_columns(layer_data)
            cleanup_and_sort(layer_data)
            
            # ── Partition by CD and stream to per-CD temp CSVs ─────────────
            if 'CD' in layer_data.columns:
                for cd_level in config['cd_levels']:
                    cd_chunk = layer_data[layer_data['CD'] == cd_level]
                    if not cd_chunk.empty:
                        temp_path = site_cd_temp_files[cd_level]
                        _append_layer_to_temp_csv(cd_chunk, temp_path)
                        logger.info(f"  Layer {layer} CD={cd_level}: appended {len(cd_chunk)} rows to temp CSV")
            else:
                logger.warning(f"CD column not found in layer {layer} data — skipping CD partition")
            
            # Free layer DataFrame immediately; GC prompt release
            del layer_data
            gc.collect()
            logger.info(f"Layer {layer} freed from memory after CD partition")

            # ── Update checkpoint after each successfully completed layer ──
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

        # Finalise each CD level from its accumulated temp CSV
        logger.info("="*60)
        logger.info(f"Finalizing SDTT data for site {site} from per-layer temp CSVs")
        logger.info("="*60)
        finalize_site_data(processor, site_cd_temp_files, layerList, site, config)

        # ── Remove checkpoint on successful completion ─────────────────────
        if os.path.exists(checkpoint_path):
            try:
                os.remove(checkpoint_path)
                logger.info(f"Checkpoint removed — {site} run complete")
            except Exception as _ce:
                logger.warning(f"Could not remove checkpoint: {_ce}")

        # Clean up per-CD temp files
        for cd_level, temp_path in site_cd_temp_files.items():
            if os.path.exists(temp_path):
                os.remove(temp_path)
                logger.info(f"Cleaned up temp file: {temp_path}")
    
    logger.info("="*80)
    logger.info("SDTT Data Processing Script Completed Successfully")
    logger.info("="*80)

def _read_sql_retry(sql: str, database_connection: str,
                    max_retries: int = 3, backoff_base: int = 30) -> pd.DataFrame:
    """Execute a SQL query via PyUber with automatic retry on transient errors.

    Opens a fresh PyUber connection for each attempt and closes it in a
    try/finally so no handles leak into Python.NET’s GC table.  This prevents
    both the System.AccessViolationException (from unclosed handles) and
    silent data loss from System.ServiceModel.CommunicationException (UBER
    WCF server drops the connection mid-query after long idle periods).

    Parameters
    ----------
    sql                : SQL string to execute
    database_connection: PyUber DSN string (e.g. 'D1D_PROD_XEUS_LOCAL')
    max_retries        : total attempts before re-raising (default 3)
    backoff_base       : seconds to wait before attempt 2, doubled each time
                         (30s → 60s → 120s by default)
    """
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


def process_layer_data(processor, data_processor, layer, all_cd_aliases_str, all_wec_aliases_str,
                      all_cd_aliases, all_wec_aliases, site):
    """Process data for a single layer."""
    logger = logging.getLogger(__name__)

    # Get operation aliases
    logger.info("Executing operalias query...")
    df_operalias = _read_sql_retry(
        QueryBuilder.operalias_query(all_cd_aliases_str, site), processor.database_connection)
    logger.info(f"Operation aliases retrieved: {len(df_operalias)} records")

    mops = df_operalias.OPERATION.to_list()
    mops_str = ','.join(f"'{item}'" for item in mops)
    processor.df_to_csv(df_operalias, f'cd_oper_alias_{site}_{layer}')
    logger.debug(f"MOPs: {mops}")

    # Get SPC lot prefetch
    logger.info("Executing SPC lot prefetch query...")
    df_spclot_prefetch = _read_sql_retry(
        QueryBuilder.spclot_prefetch_query(processor.config['days'], mops_str, site),
        processor.database_connection)
    lots = df_spclot_prefetch.LOT.drop_duplicates().to_list()
    logger.info(f"SPC lots retrieved: {len(lots)} unique lots from {len(df_spclot_prefetch)} records")
    logger.info(f"Lot list: {lots}")
    processor.df_to_csv(df_spclot_prefetch, f'spc_lot_prefetch_{site}_{layer}')

    # Get process operation aliases (WEC)
    logger.info("Executing process operation aliases query...")
    df_process_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(all_wec_aliases_str, site),
        processor.database_connection)
    logger.info(f"Process operation aliases retrieved: {len(df_process_op_aliases)} records")
    processor.df_to_csv(df_process_op_aliases, f'process_op_aliases_{site}_{layer}')
    wec_op_str = ','.join(f"'{item}'" for item in df_process_op_aliases.OPERATION.to_list())

    # Extract different operation types for minimal queries
    logger.info("Extracting operation types for minimal queries...")
    sed_aliases       = [alias for alias in all_wec_aliases if 'SED' in alias]
    hm_etch_aliases   = [alias for alias in all_wec_aliases if 'HM_ETCH' in alias]
    main_etch_aliases = [alias for alias in all_wec_aliases if 'MAIN_ETCH' in alias]

    sed_aliases_str  = ','.join(f"'{item}'" for item in sed_aliases)
    etch_aliases     = hm_etch_aliases + main_etch_aliases
    etch_aliases_str = ','.join(f"'{item}'" for item in etch_aliases)

    logger.info(f"SED aliases: {sed_aliases}")
    logger.info(f"HM_ETCH aliases: {hm_etch_aliases}")
    logger.info(f"MAIN_ETCH aliases: {main_etch_aliases}")

    # Get operation aliases for SED operations
    logger.info("Executing SED operation aliases query...")
    df_sed_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(sed_aliases_str, site),
        processor.database_connection)
    logger.info(f"SED operation aliases retrieved: {len(df_sed_op_aliases)} records")
    processor.df_to_csv(df_sed_op_aliases, f'sed_op_aliases_{site}_{layer}')
    sed_op_str = ','.join(f"'{item}'" for item in df_sed_op_aliases.OPERATION.to_list())

    # Get operation aliases for ETCH operations
    logger.info("Executing ETCH operation aliases query...")
    df_etch_op_aliases = _read_sql_retry(
        QueryBuilder.process_op_aliases_query(etch_aliases_str, site),
        processor.database_connection)
    logger.info(f"ETCH operation aliases retrieved: {len(df_etch_op_aliases)} records")
    processor.df_to_csv(df_etch_op_aliases, f'etch_op_aliases_{site}_{layer}')
    etch_op_str = ','.join(f"'{item}'" for item in df_etch_op_aliases.OPERATION.to_list())

    # Combine for passing to chunk processing
    df_minimal_op_aliases = pd.concat([df_sed_op_aliases, df_etch_op_aliases], ignore_index=True)
    processor.df_to_csv(df_minimal_op_aliases, f'minimal_op_aliases_combined_{site}_{layer}')

    # Process in chunks
    # Use a list of chunk DataFrames and concat once at the end — avoids the
    # quadratic memory growth caused by repeated pd.concat inside the loop.
    sdtt_chunks = []
    lot_chunks  = DataProcessor.chunk_list(lots, processor.config['nLots_chunk'])
    logger.info(f"Processing {len(lots)} lots in {len(lot_chunks)} chunks of {processor.config['nLots_chunk']}")

    for chunk_num, lot_chunk in enumerate(lot_chunks):
        logger.info(f"Processing chunk {chunk_num+1} of {len(lot_chunks)} ({len(lot_chunk)} lots)")
        logger.debug(f"Chunk lots: {lot_chunk}")

        lot_chunk_str = ','.join(f"'{item}'" for item in lot_chunk)

        chunk_data = process_chunk_data(
            processor, data_processor, lot_chunk_str, mops_str, wec_op_str,
            sed_op_str, etch_op_str, df_operalias, df_process_op_aliases,
            df_minimal_op_aliases, all_cd_aliases, all_wec_aliases, chunk_num, site)

        sdtt_chunks.append(chunk_data)
        logger.info(f"Chunk {chunk_num+1} completed. Running total: {sum(len(c) for c in sdtt_chunks)} records")

    # Single concat after all chunks — much more memory-efficient than accumulating inline
    logger.info(f"Combining {len(sdtt_chunks)} chunks for layer {layer}...")
    SDTT = pd.concat(sdtt_chunks, ignore_index=True)
    del sdtt_chunks
    gc.collect()

    # Finalize layer processing
    logger.info(f"Layer {layer} processing complete. Total records: {len(SDTT)}")
    return finalize_layer_data(processor, SDTT, df_spclot_prefetch, layer)

def process_chunk_data(processor, data_processor, lot_chunk_str, mops_str, wec_op_str,
                      sed_op_str, etch_op_str, df_operalias, df_process_op_aliases,
                      df_minimal_op_aliases, all_cd_aliases, all_wec_aliases, chunk_num, site):
    """Process data for a single chunk.

    All DB queries are executed via _read_sql_retry(), which opens a fresh
    PyUber connection per call, retries on transient CommunicationException,
    and always closes the connection in a try/finally.
    """
    logger = logging.getLogger(__name__)
    db = processor.database_connection   # shorthand

    # Get lot run card data
    logger.info("Executing lot run card query...")
    df_lot_run_card = _read_sql_retry(QueryBuilder.lot_run_card_query(lot_chunk_str, mops_str), db)
    spcs = df_lot_run_card.SPCS_ID.drop_duplicates().to_list()
    spcs_id_str = ','.join(f"{item}" for item in spcs)
    logger.info(f"Lot run card data retrieved: {len(df_lot_run_card)} records, {len(spcs)} unique SPCS IDs")

    # Guard against invalid SQL (WHERE ... IN ()) when chunk has no SPCS IDs.
    if len(spcs) == 0:
        logger.warning("No SPCS IDs found for this chunk; skipping chunk to avoid empty IN() SQL")
        return pd.DataFrame()

    processor.df_to_csv(df_lot_run_card, f'lot_run_card_for_spcsid_{site}')

    # Get and process measurements data
    logger.info("Executing SPC measurements query...")
    df_measurements_raw = _read_sql_retry(
        QueryBuilder.spc_measurements_no_attr_split_query(spcs_id_str), db)
    logger.info(f"SPC measurements retrieved: {len(df_measurements_raw)} raw records")

    processor.df_to_csv(df_measurements_raw, f'measurements_no_pivot_or_attr_split_{site}')

    # Extract unique wafers from measurements data for WEC query optimization
    measured_wafers = df_measurements_raw['WAFER_ID'].drop_duplicates().to_list()
    wafer_chunk_str = ','.join(f"'{item}'" for item in measured_wafers)
    logger.info(f"Extracted {len(measured_wafers)} unique wafers from measurements data for WEC filtering")
    logger.debug(f"Measured wafers: {measured_wafers}")

    # Avoid downstream WEC queries with empty wafer filter sets.
    if len(measured_wafers) == 0:
        logger.warning("No measured wafers found for this chunk; skipping chunk to avoid empty wafer IN() SQL")
        return pd.DataFrame()

    df_measurements_pivot = data_processor.process_measurements_data(df_measurements_raw, processor)
    processor.df_to_csv(df_measurements_pivot, f'spc_measurements_pivot_{site}')
    del df_measurements_raw  # raw data no longer needed; free memory

    # Get and process allstats data
    logger.info("Executing allstats query...")
    df_allstats_raw = _read_sql_retry(QueryBuilder.allstats_query(spcs_id_str), db)
    logger.info(f"Allstats data retrieved: {len(df_allstats_raw)} raw records")

    processor.df_to_csv(df_allstats_raw, f'allstats_no_pivot_or_attr_split_{site}')
    df_allstats_pivot = data_processor.process_allstats_data(df_allstats_raw, processor)
    processor.df_to_csv(df_allstats_pivot, f'allstats_pivot_chunk{chunk_num+1}_{site}')
    del df_allstats_raw  # raw data no longer needed; free memory

    # Get and process statistics data
    logger.info("Executing statistics query...")
    df_statistics_raw = _read_sql_retry(QueryBuilder.statistics_query(spcs_id_str), db)
    logger.info(f"Statistics data retrieved: {len(df_statistics_raw)} raw records")

    processor.df_to_csv(df_statistics_raw, f'spc_statistics_{site}')
    df_statistics_pivot = data_processor.process_statistics_data(df_statistics_raw, processor)
    processor.df_to_csv(df_statistics_pivot, f'statistics_pivot_chunk{chunk_num+1}_{site}')
    del df_statistics_raw  # raw data no longer needed; free memory
    gc.collect()

    # Get WEC data - optimized with wafer filtering
    logger.info("Executing optimized WEC query with wafer filtering...")
    df_wec_subop = _read_sql_retry(
        QueryBuilder.wec_query_optimized(lot_chunk_str, wec_op_str, wafer_chunk_str, site), db)
    logger.info(f"WEC data retrieved (filtered by {len(measured_wafers)} wafers): {len(df_wec_subop)} raw records")

    processor.df_to_csv(df_wec_subop, f'wec_subop_{site}')

    # Get SED data (SCANNER and RETICLE)
    logger.info("Executing SED query for SCANNER and RETICLE...")
    df_wec_sed = _read_sql_retry(
        QueryBuilder.wec_query_sed_only(lot_chunk_str, sed_op_str, wafer_chunk_str, site), db)
    logger.info(f"SED WEC data retrieved: {len(df_wec_sed)} records")

    # Get ETCH data (SUBENTITY for AME_ETCH and GTO_ETCH)
    logger.info("Executing ETCH query for SUBENTITY...")
    df_wec_etch = _read_sql_retry(
        QueryBuilder.wec_query_etch_only(lot_chunk_str, etch_op_str, wafer_chunk_str, site), db)
    logger.info(f"ETCH WEC data retrieved: {len(df_wec_etch)} records")
    
    processor.df_to_csv(df_wec_sed, f'wec_sed_{site}')
    processor.df_to_csv(df_wec_etch, f'wec_etch_{site}')
    
    # Process the minimal WEC data
    df_wec_minimal_processed = process_minimal_wec_data_separate(df_wec_sed, df_wec_etch, df_minimal_op_aliases)
    
    # Join minimal WEC data with main WEC data
    logger.info("Joining minimal WEC data with main WEC data...")
    df_wec_subop_enhanced = pd.merge(df_wec_subop, df_wec_minimal_processed, on='WAFER_ID', how='left')
    logger.info(f"Enhanced WEC data: {len(df_wec_subop_enhanced)} records")
    
    processor.df_to_csv(df_wec_subop_enhanced, f'wec_subop_enhanced_{site}')
    
    # Filter WEC data
    desired_operations = ['Process-1', 'Chuck-1']
    df_wec_subop_enhanced = df_wec_subop_enhanced[df_wec_subop_enhanced['SUB_OPERATION'].isin(desired_operations)]
    logger.info(f"WEC data filtered to desired operations: {len(df_wec_subop_enhanced)} records")
    
    df_wec = pd.merge(df_process_op_aliases, df_wec_subop_enhanced, on='OPERATION', how='inner')
    df_wec.rename(columns={'ALIAS': 'WEC_ALIAS'}, inplace=True)
    logger.info(f"WEC data merged with aliases: {len(df_wec)} records")
    
    processor.df_to_csv(df_wec, f'wec_without_subops_{site}')
    
    # Join all data
    logger.info("Joining all chunk data...")
    chunk_result = join_chunk_data(processor, df_allstats_pivot, df_statistics_pivot, 
                          df_measurements_pivot, df_wec, df_operalias, 
                          all_cd_aliases, all_wec_aliases)
    
    logger.info(f"Chunk data joined: {len(chunk_result)} final records")
    return chunk_result

def process_minimal_wec_data_separate(df_wec_sed, df_wec_etch, df_minimal_op_aliases):
    """Process separate SED and ETCH WEC data"""
    logger = logging.getLogger(__name__)
    
    # Process SED data for SCANNER and RETICLE
    sed_processed = df_wec_sed.groupby('WAFER_ID').agg({
        'SCANNER': 'first',
        'RETICLE': 'first'
    }).reset_index()
    sed_processed.columns = ['WAFER_ID', 'SCANNER_MINIMAL', 'RETICLE_MINIMAL']
    
    # Process ETCH data for AME_ETCH and GTO_ETCH
    if not df_wec_etch.empty:
        # Merge with operation aliases to identify operation types
        df_etch_with_aliases = pd.merge(df_wec_etch, df_minimal_op_aliases[['OPERATION', 'ALIAS']], 
                                       on='OPERATION', how='left')
        
        # Separate HM_ETCH and MAIN_ETCH
        hm_etch_data = df_etch_with_aliases[df_etch_with_aliases['ALIAS'].str.contains('HM_ETCH', na=False)]
        main_etch_data = df_etch_with_aliases[df_etch_with_aliases['ALIAS'].str.contains('MAIN_ETCH', na=False)]
        
        # Process each type
        ame_etch = hm_etch_data.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        ame_etch.columns = ['WAFER_ID', 'AME_ETCH']
        
        gto_etch = main_etch_data.groupby('WAFER_ID')['SUBENTITY'].first().reset_index()
        gto_etch.columns = ['WAFER_ID', 'GTO_ETCH']
        
        # Combine all etch data
        etch_processed = pd.merge(ame_etch, gto_etch, on='WAFER_ID', how='outer')
    else:
        etch_processed = pd.DataFrame(columns=['WAFER_ID', 'AME_ETCH', 'GTO_ETCH'])
    
    # Combine SED and ETCH data
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
    """Join all chunk data together"""
    logger = logging.getLogger(__name__)
    
    # Join allstats and statistics
    logger.debug("Joining allstats and statistics data...")
    common_cols = ['SPCS_ID', 'TEST_NAME', 'WAFER_ID']
    allstats_df = df_allstats_pivot.rename(columns={col: 'ALLSTATS_' + col 
                                                   for col in df_allstats_pivot.columns 
                                                   if col not in common_cols})
    statistics_df = df_statistics_pivot.rename(columns={col: 'STATISTICS_' + col 
                                                       for col in df_statistics_pivot.columns 
                                                       if col not in common_cols})
    df_sj = pd.merge(allstats_df, statistics_df, on=common_cols, how='outer')
    processor.df_to_csv(df_sj, f'spc_join_{processor.site}')
    logger.debug(f"Allstats + Statistics join: {len(df_sj)} records")
    
    # Join with measurements
    logger.debug("Joining with measurements data...")
    df_measurements = df_measurements_pivot.rename(columns={col: 'MEASUREMENTS_' + col 
                                                          for col in df_measurements_pivot.columns 
                                                          if col not in common_cols})
    df_sj2 = pd.merge(df_sj, df_measurements, on=common_cols, how='outer')
    processor.df_to_csv(df_sj2, f'spc_join2_{processor.site}')
    logger.debug(f"+ Measurements join: {len(df_sj2)} records")
    
    # Join with operation aliases - Alternative approach
    logger.debug("Joining with operation aliases...")
    df_alias2op_spc = df_operalias[['OPERATION', 'ALIAS']].rename(
        columns={'OPERATION': 'ALLSTATS_OPERATION', 'ALIAS': 'ALLSTATS_ALIAS'})
    df_sj3 = pd.merge(df_sj2, df_alias2op_spc, on=['ALLSTATS_OPERATION'], how='inner')
    df_sj3.rename(columns={'ALLSTATS_ALIAS': 'SPC_ALIAS'}, inplace=True)
    logger.debug(f"+ Operation aliases join: {len(df_sj3)} records")
    
    # Map WEC aliases
    logger.debug("Mapping WEC aliases...")
    my_dict = dict(zip(all_cd_aliases, all_wec_aliases))
    df_sj3['WEC_ALIAS'] = df_sj3['SPC_ALIAS'].map(my_dict)
    processor.df_to_csv(df_sj3, f'spc_join3_{processor.site}')
    
    # Final join with WEC data
    logger.debug("Final join with WEC data...")
    common_cols = ['WEC_ALIAS', 'WAFER_ID']
    df_wec_merge = df_wec.rename(columns={col: 'WEC_' + col 
                                         for col in df_wec.columns 
                                         if col not in common_cols})
    df_sdtt = pd.merge(df_sj3, df_wec_merge, on=common_cols, how='inner')
    processor.df_to_csv(df_sdtt, f'sdtt_chunk_{processor.site}')
    logger.debug(f"Final join result: {len(df_sdtt)} records")
    
    return df_sdtt

def finalize_layer_data(processor, SDTT, df_spclot_prefetch, layer):
    """Finalize processing for a single layer"""
    logger = logging.getLogger(__name__)
    
    logger.info(f"Finalizing layer {layer} data...")
    
    # Prepare spclot prefetch data
    spclot_prefetch_cols_to_keep = ['LOT7', 'LOT_TYPE', 'PRODUCT DEVREVSTEP', 'DATA_COLLECTION_TIME']
    df_spclot_prefetch.columns = ['SPC_' + col if col not in spclot_prefetch_cols_to_keep else col 
                                 for col in df_spclot_prefetch.columns]
    
    # Add SPC columns to SDTT
    SDTT['SPC_LOT'] = SDTT['ALLSTATS_LOT'].copy()
    SDTT['SPC_OPERATION'] = SDTT['ALLSTATS_OPERATION'].copy()
    
    # Final merge
    SDTT_fin = pd.merge(SDTT, df_spclot_prefetch, on=['SPC_LOT', 'SPC_OPERATION'], how='inner')
    logger.info(f"Layer {layer} finalized: {len(SDTT_fin)} records")
    
    # Always write the per-layer SDTT_M CSV unconditionally (bypass debug_writes gate).
    # These files are the source for --resume checkpoint recovery; suppressing them
    # would prevent resume from working even when debug_writes=False.
    _layer_csv = os.path.join(processor.folder_path, f'SDTT_M{layer}_{processor.site}.csv')
    SDTT_fin.to_csv(_layer_csv, index=False)
    logger.debug(f"Layer checkpoint written: {_layer_csv}")
    return SDTT_fin

def finalize_site_data(processor, site_cd_temp_files, layerList, site, config):
    """Read per-CD temp CSVs (built layer-by-layer in main()), run dedup/update
    logic via save_cd_level_data(), and trigger the APC join if applicable.

    Parameters
    ----------
    site_cd_temp_files : dict
        {cd_level: absolute_path_to_temp_csv}  — written during the layer loop.
    """
    logger = logging.getLogger(f"sDTT_{site}_ENGINE")
    
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

    # ── Write run manifest for incremental APC join ───────────────────────────
    # Always written regardless of debug_writes so that run_apc_join() can
    # limit its DB queries to only the wafers processed in this pipeline run.
    # Contains distinct WAFER_ID, DATA_COLLECTION_TIME, WEC_OPERATION from the
    # current run's HCCD data; consumed by run_apc_join(wafer_manifest_path=...).
    manifest_path = None
    if site in ('D1V', 'F32') and 'HCCD' in config['cd_levels']:
        _hccd_temp = site_cd_temp_files.get('HCCD')
        if _hccd_temp and os.path.exists(_hccd_temp):
            try:
                _manifest_cols = ['WAFER_ID', 'DATA_COLLECTION_TIME', 'WEC_OPERATION']
                _hccd_tmp_df = pd.read_csv(_hccd_temp,
                                           usecols=lambda c: c in _manifest_cols)
                _manifest_df = _hccd_tmp_df[
                    [c for c in _manifest_cols if c in _hccd_tmp_df.columns]
                ].drop_duplicates()
                manifest_path = os.path.join(config['main_csv_path'],
                                             f'current_run_wafers_{site}.csv')
                _manifest_df.to_csv(manifest_path, index=False)
                logger.info(f"Run manifest written: {manifest_path} "
                            f"({len(_manifest_df)} distinct rows)")
                del _hccd_tmp_df, _manifest_df
                gc.collect()
            except Exception as _me:
                logger.warning(f"Could not write run manifest "
                               f"(APC join will use full-file mode): {_me}")
                manifest_path = None
        else:
            logger.warning("HCCD temp CSV not available — manifest not written; "
                           "APC join will use full-file mode")

    # ── APC enrichment (D1V/F32 + HCCD) ───────────────────────────────────────
    # After all CD-level CSVs are saved, run the APC join on the site HCCD output.
    # Also run on the 60-day variant if it was created.
    # Failure here is non-fatal — the base CSV is already saved and the error is logged.
    # Set config['skip_apc_join'] = True to suppress (e.g. via pipeline --skip-apc).
    if site in ('D1V', 'F32') and 'HCCD' in config['cd_levels'] and not config.get('skip_apc_join', False):
        _apc_variants = [
            f"{config['main_csv_base_name']}_HCCD_{site}.csv",  # Full dataset
            f"{config['main_csv_base_name']}_HCCD_{site}_60day.csv",  # 60-day variant
        ]
        for apc_input in _apc_variants:
            apc_input_path = os.path.join(config['main_csv_path'], apc_input)
            if os.path.exists(apc_input_path):
                logger.info("="*60)
                logger.info(f"Starting APC join for {site}+HCCD: {apc_input_path}")
                logger.info("="*60)
                try:
                    import importlib.util as _ilu
                    _apc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             '1278sDTT_D1V_HCCD_APC_JOIN.py')
                    _spec = _ilu.spec_from_file_location('_apc_join_mod', _apc_path)
                    _apc_mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_apc_mod)
                    # Cold-start 60-day: no existing APC file, so skip the manifest and
                    # use a 60-day lookback so all wafers in the source window are queried.
                    # Normal incremental runs use the manifest as usual.
                    _is_60day = apc_input.endswith('_60day.csv')
                    _existing_apc = apc_input_path.replace('.csv', '_APC.csv')
                    _60day_cold = _is_60day and not os.path.exists(_existing_apc)
                    _use_manifest = None if _60day_cold else manifest_path
                    _use_lookback = 60 if _60day_cold else config.get('apc_query_lookback_days')
                    if _60day_cold:
                        logger.info('60-day cold-start: running full-file APC join (no manifest, 60-day lookback)')
                    out = _apc_mod.run_apc_join(apc_input_path,
                                                wafer_manifest_path=_use_manifest,
                                                apc_query_lookback_days=_use_lookback,
                                                require_area_btool_for_match_ops=config.get('require_area_btool_for_match_ops'),
                                                site=site)
                    logger.info(f"APC join completed. Output: {out}")
                except Exception as _e:
                    logger.error(f"APC join failed for {apc_input} (non-fatal, base CSV is intact): {_e}", exc_info=True)
            elif apc_input.endswith('_60day.csv'):
                logger.info(f"60-day HCCD variant not yet created — skipping APC join for {apc_input}")
            else:
                logger.warning(f"APC join skipped — HCCD_{site} CSV not found: {apc_input_path}")

def save_cd_level_data(processor, cd_data_new, csv_name, cd_level, site):
    """Save CD level data, handling existing CSV and removing unnecessary columns"""
    logger = logging.getLogger(f"sDTT_{site}_ENGINE")
    prune_targets = {('DCCD', 'D1V'), ('DCCD', 'F32'), ('FCCD', 'D1V'), ('FCCD', 'F32')}
    retention_days = 90
    
    # Define columns to remove for each CD level
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
            'STATISTICS_WAFER_SIGMA_VIOLATED_RULE_NOTATION'
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
            'STATISTICS_SIGMA_DTT_VALUE'
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
            'STATISTICS_WAFER_SIGMA_VIOLATED_RULE_NOTATION'
        ]
    }
    
    # Remove unnecessary columns for this CD level
    if cd_level in columns_to_remove:
        cols_to_remove = columns_to_remove[cd_level]
        # Only remove columns that actually exist in the dataframe
        existing_cols_to_remove = [col for col in cols_to_remove if col in cd_data_new.columns]
        
        if existing_cols_to_remove:
            cd_data_new = cd_data_new.drop(columns=existing_cols_to_remove)
            logger.info(f"Removed {len(existing_cols_to_remove)} unnecessary columns from {cd_level}_{site} data")
            logger.debug(f"Removed columns: {existing_cols_to_remove}")
        else:
            logger.info(f"No columns to remove for {cd_level}_{site} (none of the specified columns exist)")
    
    # Load existing data (returns empty DataFrame if file doesn't exist)
    cd_data_old = processor.main_csv_to_df(csv_name)
    
    if not cd_data_old.empty:
        logger.info(f"Found existing data for {cd_level}_{site}: {len(cd_data_old)} records")
        
        # Also remove unnecessary columns from existing data if they exist
        if cd_level in columns_to_remove:
            cols_to_remove = columns_to_remove[cd_level]
            existing_cols_to_remove_old = [col for col in cols_to_remove if col in cd_data_old.columns]
            
            if existing_cols_to_remove_old:
                cd_data_old = cd_data_old.drop(columns=existing_cols_to_remove_old)
                logger.info(f"Removed {len(existing_cols_to_remove_old)} unnecessary columns from existing {cd_level}_{site} data")
        
        # Remove duplicates from old data based on WAFER_ID and TEST_NAME.
        # Merge-based approach: O(n+m) vs the old row-wise apply which was O(n*m)
        # and allocated a full boolean matrix on every row — very expensive for
        # large existing CSVs.
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
        
        # Combine old and new data
        cd_data_final = pd.concat([cd_data_old_filtered, cd_data_new], ignore_index=True)
        logger.info(f"Combined {cd_level}_{site} data: {len(cd_data_old_filtered)} existing + {len(cd_data_new)} new = {len(cd_data_final)} total")
    else:
        # No existing data, use new data only
        cd_data_final = cd_data_new
        logger.info(f"No existing data found for {cd_level}_{site}. Using new data only: {len(cd_data_final)} records")
    
    cd_data_final.reset_index(drop=True, inplace=True)

    # ── Sort full combined table (all layers + any existing rows) ─────────────
    # DATA_COLLECTION_TIME arrives as a string when read back from the temp CSV
    # or when loaded from an existing on-disk CSV, so we parse before sorting.
    if 'DATA_COLLECTION_TIME' in cd_data_final.columns:
        cd_data_final['DATA_COLLECTION_TIME'] = pd.to_datetime(
            cd_data_final['DATA_COLLECTION_TIME'],
            format='mixed', dayfirst=False, errors='coerce'
        )
        cd_data_final.sort_values(by='DATA_COLLECTION_TIME', ascending=False, inplace=True)
        cd_data_final.reset_index(drop=True, inplace=True)
        logger.info(f"Sorted {cd_level}_{site} final table by DATA_COLLECTION_TIME (descending)")

        if (cd_level, site) in prune_targets:
            cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=retention_days)
            rows_before_prune = len(cd_data_final)
            valid_time_mask = cd_data_final['DATA_COLLECTION_TIME'].notna()
            retained_rows_mask = valid_time_mask & (cd_data_final['DATA_COLLECTION_TIME'] >= cutoff)
            dropped_null_dates = rows_before_prune - int(valid_time_mask.sum())
            cd_data_final = cd_data_final.loc[retained_rows_mask].reset_index(drop=True)
            rows_pruned = rows_before_prune - len(cd_data_final)
            logger.info(
                f"Pruned {rows_pruned} rows older than {retention_days} days from {cd_level}_{site} "
                f"using DATA_COLLECTION_TIME cutoff {cutoff:%Y-%m-%d %H:%M:%S}"
            )
            if dropped_null_dates:
                logger.warning(
                    f"Dropped {dropped_null_dates} {cd_level}_{site} rows with unparsable DATA_COLLECTION_TIME"
                )
    elif (cd_level, site) in prune_targets:
        logger.warning(
            f"Skipping {retention_days}-day retention pruning for {cd_level}_{site}: "
            "DATA_COLLECTION_TIME column is missing"
        )

    # Save final data
    processor.main_df_to_csv(cd_data_final, csv_name, 1)
    
    today = date.today()
    date_string = today.strftime("%Y-%m-%d")
    final_message = f"CSV created/updated for {cd_level}_{site}: {date_string} {csv_name}.csv ({len(cd_data_final)} records)"
    print(final_message)
    logger.info(final_message)

    # ── Write 60-day pruned HCCD variant for JMP reporting ──────────────────
    # For HCCD D1V and F32, also create a 60-day pruned version to improve
    # JMP reporting performance. The full retention (200+ days) is preserved above.
    if cd_level == 'HCCD' and site in ('D1V', 'F32') and 'DATA_COLLECTION_TIME' in cd_data_final.columns:
        hccd_60day_cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=60)
        hccd_valid_time_mask = cd_data_final['DATA_COLLECTION_TIME'].notna()
        hccd_retained_mask = hccd_valid_time_mask & (cd_data_final['DATA_COLLECTION_TIME'] >= hccd_60day_cutoff)
        cd_data_60day = cd_data_final.loc[hccd_retained_mask].reset_index(drop=True)
        hccd_60day_name = f"{csv_name}_60day"
        processor.main_df_to_csv(cd_data_60day, hccd_60day_name, 1)
        hccd_60day_msg = f"60-day HCCD variant created for {site}: {len(cd_data_60day)} records (cutoff: {hccd_60day_cutoff:%Y-%m-%d})"
        logger.info(hccd_60day_msg)

def _append_layer_to_temp_csv(df: pd.DataFrame, path: str) -> None:
    """Append *df* to a per-CD temp CSV, guaranteeing column alignment.

    Different layers may produce different column sets (e.g. BM0 omits some
    MEASUREMENTS_WAFER_RADIUS columns that numbered layers include).  A naive
    csv append produces rows with more or fewer fields than the header, causing
    pandas.errors.ParserError on read-back.

    Strategy
    --------
    * First write  : write with header; done.
    * Later writes : read the existing header (nrows=0) to get the established
      column list.  Compute the union of existing + new columns.
        - If the new chunk introduces extra columns: rewrite the existing file
          so those columns are included (NaN-filled for prior rows).
        - Reindex the new chunk to the full union (NaN for any absent cols).
      Then append without header — every row now has the same field count.
    """
    logger = logging.getLogger(__name__)

    if not os.path.exists(path):
        df.to_csv(path, index=False)
        return

    # Read existing header only — very cheap regardless of file size
    existing_cols = pd.read_csv(path, nrows=0).columns.tolist()
    new_cols      = df.columns.tolist()
    union_cols    = existing_cols + [c for c in new_cols if c not in existing_cols]

    if len(union_cols) > len(existing_cols):
        # New layer introduced extra columns — rewrite existing rows with NaN for them
        extra = [c for c in union_cols if c not in existing_cols]
        logger.debug(f"Temp CSV column set expanded by {len(extra)} columns: {extra} — rewriting")
        existing_df = pd.read_csv(path, low_memory=False)
        existing_df = existing_df.reindex(columns=union_cols)
        existing_df.to_csv(path, index=False)

    # Align new chunk to the full union and append (no header)
    df.reindex(columns=union_cols).to_csv(path, mode='a', header=False, index=False)


def add_esc_zones(SDTT):
    """Add ESC zone calculations"""
    logger = logging.getLogger(__name__)
    
    zone_columns_added = 0
    for col_idx, col_name in enumerate(SDTT.columns):
        if 'WAFER_RADIUS' in col_name:
            new_col_name = f'{col_name}_ZONE'
            conditions = [
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] < 38),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 38) & (SDTT[col_name] < 108),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 108) & (SDTT[col_name] < 128.5),
                (SDTT['WEC_ENTITY_PREFIX'] == 'AME') & (SDTT[col_name] >= 128.5)
            ]
            choices = ['I', 'MI', 'MO', 'O']
            SDTT[new_col_name] = np.select(conditions, choices, default='')
            zone_columns_added += 1
    
    logger.debug(f"Added {zone_columns_added} ESC zone columns")

def add_derived_columns(SDTT):
    """Add derived combination columns.

    PROD_MOP       : PRODUCT + '_' + ALLSTATS_OPERATION
    PROD_MOP_PILOT : PRODUCT + '_' + ALLSTATS_OPERATION + '_' + ALLSTATS_PILOT_NAME

    Must be called AFTER rename_final_columns() so that 'PRODUCT' already exists
    (it is renamed from 'PRODUCT DEVREVSTEP' in that step).
    """
    logger = logging.getLogger(__name__)

    op_col     = 'ALLSTATS_OPERATION'
    prod_col   = 'PRODUCT'
    pilot_col  = 'ALLSTATS_PILOT_NAME'

    if op_col in SDTT.columns and prod_col in SDTT.columns:
        SDTT['PROD_MOP'] = (
            SDTT[prod_col].astype(str) + '_' + SDTT[op_col].astype(str)
        )
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
    """Rename columns to final names"""
    logger = logging.getLogger(__name__)
    
    column_renames = {
        'ALLSTATS_PRODUCT_GROUP': 'PRODUCT_GROUP',
        'PRODUCT DEVREVSTEP': 'PRODUCT',
        'ALLSTATS_STRUCTURE': 'STRUCTURE',
        'ALLSTATS_ROUTE_TYPE': 'ROUTE_TYPE',
        'ALLSTATS_IS_POR': 'IS_POR',
        'MEASUREMENTS_WAFER_RECIPE': 'SPC_RECIPE',
        'WEC_WAFER_SHORT': 'WID',
        'WEC_DB_BATCH_ID': 'DB_BATCH_ID',
        'ALLSTATS_PRIMARY_ENTITY': 'PRIMARY_ENTITY',
        'ALLSTATS_ANALYTICAL_ENTITY': 'ANALYTICAL_ENTITY',
        'WEC_SUBENTITY': 'SUBENTITY',
        'MEASUREMENTS_CD': 'CD',
        'MEASUREMENTS_LAYER': 'LAYER',
        'WEC_RETICLE_MINIMAL': 'RETICLE',      # Changed from WEC_RETICLE
        'WEC_SCANNER_MINIMAL': 'SCANNER',      # Changed from WEC_SCANNER  
        'WEC_AME_ETCH': 'AME_ETCH',
        'WEC_GTO_ETCH': 'GTO_ETCH'
    }
    
    # ... rest of function remains the same ...
    
    renamed_count = 0
    for old_name, new_name in column_renames.items():
        if old_name in SDTT.columns:
            SDTT.rename(columns={old_name: new_name}, inplace=True)
            renamed_count += 1
    
    # Copy columns
    if 'WEC_SUBENTITY_END_TIME' in SDTT.columns:
        SDTT['SUBENTITY_END_TIME'] = SDTT['WEC_SUBENTITY_END_TIME'].copy()
    if 'SPC_ROUTE' in SDTT.columns:
        SDTT['ROUTE'] = SDTT['SPC_ROUTE'].copy()
    
    logger.debug(f"Renamed {renamed_count} columns")

def reorder_columns(SDTT):
    """Reorder columns according to specification"""
    logger = logging.getLogger(__name__)
    
    columns_to_move = [
        # ── Preferred front-of-file order (user-specified) ────────────────────
        'DATA_COLLECTION_TIME',
        'SPC_LOT', 'WID', 'IS_POR', 'WAFER_ID', 'TEST_NAME', 'PRODUCT',
        'SUBENTITY', 'SUBENTITY_END_TIME',
        'WEC_OPERATION', 'WEC_RECIPE', 'WEC_LAYER',
        'ALLSTATS_MEAN_DTT_VALUE', 'ALLSTATS_MEAN_TARGET_VALUE', 'ALLSTATS_WAFER_MEAN_VALUE',
        'ALLSTATS_SIGMA_DTT_VALUE', 'ALLSTATS_SIGMA_TARGET_VALUE', 'ALLSTATS_WAFER_SIGMA_VALUE',
        'ROUTE', 'SPC_OPERATION', 'PROD_MOP', 'PROD_MOP_PILOT',
        # APC columns (D1V/HCCD only — silently skipped for other outputs)
        'APC_B_TOOL', 'APC_SETTING_USED', 'APC_OPENRUNS', 'APC_FB_SUC',
        'APC_AREA', 'APC_PRODGROUP',
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
    
    # Filter columns_to_move to only include existing columns
    existing_columns_to_move = [col for col in columns_to_move if col in current_columns]
    new_column_order = existing_columns_to_move + sorted_remaining
    SDTT = SDTT.reindex(columns=new_column_order)
    
    logger.debug(f"Reordered columns: {len(existing_columns_to_move)} priority + {len(sorted_remaining)} remaining")
    
    return SDTT

def cleanup_and_sort(SDTT):
    """Clean up unnamed columns and parse date columns.

    Note: Sorting is intentionally NOT done here.  Per-layer data is appended
    to a per-CD temp CSV and later concatenated across all layers; sorting at
    this stage would be discarded and wasted.  The definitive sort over the
    full combined table (all layers + any existing CSV rows) is applied inside
    save_cd_level_data() before the final write.
    """
    logger = logging.getLogger(__name__)
    
    # Remove unnamed columns
    unnamed_cols = SDTT.columns[SDTT.columns.str.contains('Unnamed', case=False)]
    if len(unnamed_cols) > 0:
        SDTT.drop(columns=unnamed_cols, inplace=True)
        logger.debug(f"Removed {len(unnamed_cols)} unnamed columns")
    
    # Parse date columns (needed for correct dtype; sort deferred to finalization)
    if 'SUBENTITY_END_TIME' in SDTT.columns:
        SDTT['SUBENTITY_END_TIME'] = pd.to_datetime(SDTT['SUBENTITY_END_TIME'],
                                                   format='mixed', dayfirst=False,
                                                   errors='coerce')
    if 'DATA_COLLECTION_TIME' in SDTT.columns:
        SDTT['DATA_COLLECTION_TIME'] = pd.to_datetime(SDTT['DATA_COLLECTION_TIME'],
                                                      format='mixed', dayfirst=False,
                                                      errors='coerce')
    
    SDTT.reset_index(drop=True, inplace=True)

if __name__ == "__main__":
    main()