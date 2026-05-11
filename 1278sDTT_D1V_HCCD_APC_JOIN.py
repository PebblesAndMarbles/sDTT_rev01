import re
import pandas as pd
import PyUber
import logging
from datetime import datetime
from pathlib import Path
import gc

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Silence SQLAlchemy/PyUber loggers that echo SQL text at INFO level
for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
               'sqlalchemy', 'PyUber', 'pyuber'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_PM_RE = re.compile(r'_PM(\d+)', re.IGNORECASE)

# Preferred front-of-table column order (mirrors reorder_columns in 1278sDTT_D1V_F32.py).
# APC columns are included here so they land near the front AFTER the join merges them in.
# Any column not listed lands in sorted order after these.
_PREFERRED_COLUMN_ORDER = [
    'DATA_COLLECTION_TIME',
    'SPC_LOT', 'WID', 'IS_POR', 'WAFER_ID', 'TEST_NAME', 'PRODUCT',
    'SUBENTITY', 'SUBENTITY_END_TIME',
    'WEC_OPERATION', 'WEC_RECIPE', 'WEC_LAYER',
    'ALLSTATS_MEAN_DTT_VALUE', 'ALLSTATS_MEAN_TARGET_VALUE', 'ALLSTATS_WAFER_MEAN_VALUE',
    'ALLSTATS_SIGMA_DTT_VALUE', 'ALLSTATS_SIGMA_TARGET_VALUE', 'ALLSTATS_WAFER_SIGMA_VALUE',
    'ROUTE', 'SPC_OPERATION', 'PROD_MOP', 'PROD_MOP_PILOT',
    # APC columns — now present after the join
    'APC_B_TOOL', 'APC_SETTING_USED', 'APC_OPENRUNS', 'APC_FB_SUC',
    'APC_AREA', 'APC_PRODGROUP',
    # Identity / lineage
    'ANALYTICAL_ENTITY', 'PRIMARY_ENTITY',
    'SPC_RECIPE', 'SPC_PILOT_NAME', 'SPC_ALIAS',
    'SCANNER', 'RETICLE', 'AME_ETCH', 'GTO_ETCH',
    'WEC_LOT', 'WEC_ALIAS', 'ROUTE_TYPE', 'LAYER', 'CD',
    'STRUCTURE', 'PRODUCT_GROUP', 'LOT7', 'LOT_TYPE', 'SPCS_ID', 'DB_BATCH_ID',
]

def _reorder_columns_apc(df: pd.DataFrame) -> pd.DataFrame:
    """Reorder *df* columns so preferred columns come first, remaining sorted.

    Mirrors the logic of reorder_columns() in 1278sDTT_D1V_F32.py, but runs
    AFTER the APC join so that APC columns are present and can be placed early.
    """
    def _sort_key(col, split_char='-'):
        parts = col.split(split_char)
        if len(parts) >= 3:
            return (0, parts[0], parts[1], parts[2])
        elif len(parts) >= 2:
            return (1, parts[0], parts[1], '')
        return (2, col, '', '')

    current = df.columns.tolist()
    priority = [c for c in _PREFERRED_COLUMN_ORDER if c in current]
    remaining = sorted([c for c in current if c not in _PREFERRED_COLUMN_ORDER], key=_sort_key)
    return df.reindex(columns=priority + remaining)

def _extract_pm_index(subentity_value) -> int:
    """Return 0-based PM index from e.g. 'AME425_PM3' → 2. Returns -1 on failure."""
    if pd.isna(subentity_value):
        return -1
    m = _PM_RE.search(str(subentity_value))
    return int(m.group(1)) - 1 if m else -1


def apply_ube_subentity_extraction(df_final):
    """
    For rows where APC_AREA == '8AMEUBE', the columns APC_B_TOOL, APC_B_TOOL_RS
    (semicolon-separated) and APC_OPENRUNS (comma-separated) contain values for
    all 6 sub-chambers of the parent entity.  Use the source SUBENTITY column to
    select only the value that corresponds to the chamber this row processed on.
    """
    ube_mask = df_final['APC_AREA'].isin(['8AMEUBE', '8AMEUBE_GAS'])
    n_ube = ube_mask.sum()
    if n_ube == 0:
        logger.info("UBE extraction: no 8AMEUBE / 8AMEUBE_GAS rows — skipping")
        return df_final

    logger.info(f"UBE subentity extraction: {n_ube} rows with APC_AREA in ['8AMEUBE','8AMEUBE_GAS']")

    pm_indices = df_final.loc[ube_mask, 'SUBENTITY'].apply(_extract_pm_index)
    failed = (pm_indices == -1).sum()
    if failed > 0:
        logger.warning(f"  {failed} rows could not extract PM index from SUBENTITY — packed values left unchanged")

    def _pick(packed, idx, sep):
        if pd.isna(packed) or idx < 0:
            return packed
        parts = str(packed).split(sep)
        if idx >= len(parts):
            return packed
        val = parts[idx].strip()
        return val if val not in ('', '[NULL]', 'None', 'nan') else packed

    for col, sep in [('APC_B_TOOL', ';'), ('APC_B_TOOL_RS', ';'),
                       ('APC_OPENRUNS', ',')]:
        if col not in df_final.columns:
            continue
        df_final.loc[ube_mask, col] = [
            _pick(packed, idx, sep)
            for packed, idx in zip(df_final.loc[ube_mask, col], pm_indices)
        ]
        logger.info(f"  {col}: chamber-specific value extracted for 8AMEUBE rows")

    # Verify no packed strings remain
    for col, sep in [('APC_B_TOOL', ';'), ('APC_B_TOOL_RS', ';'),
                     ('APC_OPENRUNS', ',')]:
        if col not in df_final.columns:
            continue
        still_packed = df_final.loc[ube_mask, col].astype(str).str.contains(re.escape(sep), na=False).sum()
        if still_packed > 0:
            logger.warning(f"  {col}: {still_packed} rows still contain '{sep}' — check those rows")
        else:
            logger.info(f"  {col}: ✓ all 8AMEUBE rows resolved to single values")

    return df_final


def create_apc_query_with_area(wafer_chunk_str, operation_str, apc_system, target_area):
    """Create APC query with specific area filter"""
    return f"""
    SELECT 'D1V' AS SITE,
           h.LOT7 AS LOT,
           w.WAFER AS WAFER_ID,
           h.OPERATION AS APC_OPERATION,
           h.OUT_DATE,
           j.APC_DATA_ID,
           j.APC_JOB_TXN_TIME AS TXN_DATE,
           j.APC_OBJECT_NAME,
           j.APC_OBJECT_TYPE,
           j.CHANGE_TYPE,
           d.ATTRIBUTE_NAME,
           d.ATTRIBUTE_VALUE
    FROM F_LOT_FLOW h
    INNER JOIN F_WAFERSLOTHIST w
        ON w.EXPECTED_LOT = h.LOT
        AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
        AND w.HISTORY_DELETED_FLAG = 'N'
    INNER JOIN P_APC_RUNJOB_HIST j
        ON j.LOTOPERKEY = h.LOTOPERKEY
    INNER JOIN P_APC_TXN_DATA d
        ON d.APC_DATA_ID = j.APC_DATA_ID
    WHERE w.WAFER IN ({wafer_chunk_str})
      AND h.OPERATION IN ({operation_str})
      AND h.EXEC_FLAG NOT IN ('X','R','N')
      AND j.APC_OBJECT_NAME LIKE '{apc_system}%'
      AND j.APC_OBJECT_TYPE = 'LOT'
      AND EXISTS (
          SELECT 1 FROM P_APC_TXN_DATA d_area
          WHERE d_area.APC_DATA_ID = j.APC_DATA_ID
            AND d_area.ATTRIBUTE_NAME = 'AREA'
            AND d_area.ATTRIBUTE_VALUE = '{target_area}'
      )
      AND d.ATTRIBUTE_NAME IN (
          'CALCULATED_SETTING','B_TOOL','B_TOOL_RS','FB_SUC','LOTID','M_ETCHRATE',
          'MACHINE','OPENRUNS','OPENRUNS_PART',
          'OPERATION','PROCESS_OPN','PRODGROUP','PRODUCT','SETTING_USED',
          'SUBENTITIES','SUBENTITY','AREA',
          'LAMBDA_DRIFT','LAMBDA_TOOL',
          'LAMBDA_POSTPM','LAMBDA_POSTPM_TOOL'
      )
    """

def create_apc_query_no_area(wafer_chunk_str, operation_str, apc_system):
    """Create APC query without area filter (last resort)"""
    return f"""
    SELECT 'D1V' AS SITE,
           h.LOT7 AS LOT,
           w.WAFER AS WAFER_ID,
           h.OPERATION AS APC_OPERATION,
           h.OUT_DATE,
           j.APC_DATA_ID,
           j.APC_JOB_TXN_TIME AS TXN_DATE,
           j.APC_OBJECT_NAME,
           j.APC_OBJECT_TYPE,
           j.CHANGE_TYPE,
           d.ATTRIBUTE_NAME,
           d.ATTRIBUTE_VALUE
    FROM F_LOT_FLOW h
    INNER JOIN F_WAFERSLOTHIST w
        ON w.EXPECTED_LOT = h.LOT
        AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
        AND w.HISTORY_DELETED_FLAG = 'N'
    INNER JOIN P_APC_RUNJOB_HIST j
        ON j.LOTOPERKEY = h.LOTOPERKEY
    INNER JOIN P_APC_TXN_DATA d
        ON d.APC_DATA_ID = j.APC_DATA_ID
    WHERE w.WAFER IN ({wafer_chunk_str})
      AND h.OPERATION IN ({operation_str})
      AND h.EXEC_FLAG NOT IN ('X','R','N')
      AND j.APC_OBJECT_NAME LIKE '{apc_system}%'
      AND j.APC_OBJECT_TYPE = 'LOT'
      AND d.ATTRIBUTE_NAME IN (
          'CALCULATED_SETTING','B_TOOL','B_TOOL_RS','FB_SUC','LOTID','M_ETCHRATE',
          'MACHINE','OPENRUNS','OPENRUNS_PART',
          'OPERATION','PROCESS_OPN','PRODGROUP','PRODUCT','SETTING_USED',
          'SUBENTITIES','SUBENTITY','AREA',
          'LAMBDA_DRIFT','LAMBDA_TOOL',
          'LAMBDA_POSTPM','LAMBDA_POSTPM_TOOL'
      )
    """

def try_apc_system_with_area_cascade(wafer_list, operation_str, conn, apc_system, chunk_id):
    """Try APC system with cascading area filters: AMECT_ICCR2 → 8AMEUBE → no filter"""
    
    if not wafer_list:
        return pd.DataFrame(), "no_wafers"
    
    wafer_chunk_str = "'" + "','".join(wafer_list) + "'"
    
    # Try AREA = 'AMECT_ICCR2' first
    try:
        query1 = create_apc_query_with_area(wafer_chunk_str, operation_str, apc_system, 'AMECT_ICCR2')
        df_result = pd.read_sql(query1, conn)
        if len(df_result) > 0:
            return df_result, "AMECT_ICCR2"
    except Exception as e:
        logger.warning(f"  {chunk_id} {apc_system} AMECT_ICCR2 failed: {str(e)}")
    
    # Try AREA = '8AMEUBE' as fallback
    try:
        query2 = create_apc_query_with_area(wafer_chunk_str, operation_str, apc_system, '8AMEUBE')
        df_result = pd.read_sql(query2, conn)
        if len(df_result) > 0:
            return df_result, "8AMEUBE"
    except Exception as e:
        logger.warning(f"  {chunk_id} {apc_system} 8AMEUBE failed: {str(e)}")
    
    # Last resort: no area filter
    try:
        query3 = create_apc_query_no_area(wafer_chunk_str, operation_str, apc_system)
        df_result = pd.read_sql(query3, conn)
        if len(df_result) > 0:
            return df_result, "no_area_filter"
    except Exception as e:
        logger.warning(f"  {chunk_id} {apc_system} no area filter failed: {str(e)}")
    
    return pd.DataFrame(), "failed"

def process_chunk_cascading_priority(wafer_chunk, operation_str, conn, chunk_id):
    """Process chunk with cascading area filters and APC system priority"""
    try:
        all_wafers = set(wafer_chunk)
        remaining_wafers = list(all_wafers)
        
        results = []
        area_stats = {
            'AEPCMC': {'AMECT_ICCR2': 0, '8AMEUBE': 0, 'no_area_filter': 0, 'failed': 0},
            'AEPC2': {'AMECT_ICCR2': 0, '8AMEUBE': 0, 'no_area_filter': 0, 'failed': 0}
        }
        
        # Stage 1: Try AEPCMC with area cascade
        df_aepcmc, area_used = try_apc_system_with_area_cascade(
            remaining_wafers, operation_str, conn, 'AEPCMC', chunk_id
        )
        
        if len(df_aepcmc) > 0:
            results.append(df_aepcmc)
            wafers_with_aepcmc = set(df_aepcmc['WAFER_ID'].unique())
            remaining_wafers = list(all_wafers - wafers_with_aepcmc)
            area_stats['AEPCMC'][area_used] = len(df_aepcmc)
        else:
            area_stats['AEPCMC']['failed'] = len(remaining_wafers)
        
        # Stage 2: Try AEPC2 for remaining wafers with area cascade
        df_aepc2 = pd.DataFrame()
        if remaining_wafers:
            df_aepc2, area_used = try_apc_system_with_area_cascade(
                remaining_wafers, operation_str, conn, 'AEPC2', chunk_id
            )
            
            if len(df_aepc2) > 0:
                results.append(df_aepc2)
                area_stats['AEPC2'][area_used] = len(df_aepc2)
                wafers_with_aepc2 = set(df_aepc2['WAFER_ID'].unique())
                remaining_wafers = list(set(remaining_wafers) - wafers_with_aepc2)
            
            if remaining_wafers:
                area_stats['AEPC2']['failed'] = len(remaining_wafers)
        
        # Combine results
        if results:
            df_combined = pd.concat(results, ignore_index=True)
        else:
            df_combined = pd.DataFrame()
        
        # Detailed logging
        total_records = len(df_combined)
        aepcmc_records = len(df_aepcmc) if len(df_aepcmc) > 0 else 0
        aepc2_records = len(df_aepc2) if len(df_aepc2) > 0 else 0
        
        logger.info(f"Chunk {chunk_id}: {total_records} records (AEPCMC: {aepcmc_records}, AEPC2: {aepc2_records})")
        
        # Log area usage
        for system in ['AEPCMC', 'AEPC2']:
            for area, count in area_stats[system].items():
                if count > 0:
                    logger.info(f"  {system} {area}: {count} records")
        
        return df_combined, area_stats
        
    except Exception as e:
        logger.error(f"Chunk {chunk_id} failed: {str(e)}")
        return pd.DataFrame(), {}

def safe_pivot_with_prefix_fixed(df_dedup):
    """Safely pivot data and add APC_ prefix, handling duplicate columns properly"""
    try:
        logger.info("Checking for potential pivot issues...")
        
        # Check for duplicate combinations that could cause pivot issues
        duplicate_check = df_dedup.groupby(['WAFER_ID', 'APC_OPERATION', 'ATTRIBUTE_NAME']).size()
        duplicates = duplicate_check[duplicate_check > 1]
        
        if len(duplicates) > 0:
            logger.warning(f"Found {len(duplicates)} duplicate wafer-operation-attribute combinations")
            logger.info("Removing duplicates before pivot...")
            df_dedup = df_dedup.drop_duplicates(subset=['WAFER_ID', 'APC_OPERATION', 'ATTRIBUTE_NAME'], keep='first')
        
        # Check for problematic attribute names
        attribute_names = df_dedup['ATTRIBUTE_NAME'].unique()
        logger.info(f"Unique attribute names: {len(attribute_names)}")
        
        # Check if 'OPERATION' is in attribute names (this could cause the duplicate column issue)
        if 'OPERATION' in attribute_names:
            logger.warning("Found 'OPERATION' in attribute names - this could conflict with APC_OPERATION")
            logger.info("Renaming OPERATION attribute to OPERATION_ATTR to avoid conflict")
            df_dedup.loc[df_dedup['ATTRIBUTE_NAME'] == 'OPERATION', 'ATTRIBUTE_NAME'] = 'OPERATION_ATTR'
        
        # Pivot to wide format
        logger.info("Pivoting APC data to wide format...")
        df_pivot = df_dedup.pivot_table(
            index=['WAFER_ID', 'APC_OPERATION'],
            columns='ATTRIBUTE_NAME',
            values='ATTRIBUTE_VALUE',
            aggfunc='first'  # Take first value if duplicates exist
        ).reset_index()
        
        # Clean up column names and handle the columns.name issue
        df_pivot.columns.name = None
        
        # Ensure we have clean column names
        logger.info(f"Pivot successful. Shape: {df_pivot.shape}")
        logger.info(f"Columns after pivot: {list(df_pivot.columns)}")
        
        # Add APC_ prefix to all columns except WAFER_ID and APC_OPERATION
        logger.info("Adding APC_ prefix to all APC columns...")
        new_columns = {}
        for col in df_pivot.columns:
            if col in ['WAFER_ID', 'APC_OPERATION']:
                new_columns[col] = col  # Keep these as-is
            else:
                new_columns[col] = f'APC_{col}'
        
        df_renamed = df_pivot.rename(columns=new_columns)
        
        # Verify no duplicate columns
        if len(df_renamed.columns) != len(set(df_renamed.columns)):
            logger.error("Duplicate columns detected after renaming!")
            duplicate_cols = [col for col in df_renamed.columns if list(df_renamed.columns).count(col) > 1]
            logger.error(f"Duplicate columns: {duplicate_cols}")
            
            # Fix duplicate columns by adding suffix
            df_renamed.columns = pd.Index([f"{col}_{i}" if list(df_renamed.columns).count(col) > 1 else col 
                                         for i, col in enumerate(df_renamed.columns)])
        
        # Show column mapping
        apc_columns_added = [new_col for old_col, new_col in new_columns.items() 
                           if old_col not in ['WAFER_ID', 'APC_OPERATION']]
        lambda_columns_added = [col for col in apc_columns_added if 'LAMBDA' in col]
        
        logger.info(f"APC columns added with prefix: {len(apc_columns_added)} total")
        logger.info(f"LAMBDA columns: {len(lambda_columns_added)} - {lambda_columns_added}")
        logger.info(f"Final columns: {list(df_renamed.columns)}")
        
        return df_renamed, new_columns
        
    except Exception as e:
        logger.error(f"Pivot operation failed: {str(e)}")
        logger.info("Attempting manual pivot approach...")
        
        # Manual pivot approach
        try:
            pivot_data = {}
            
            for _, row in df_dedup.iterrows():
                key = (row['WAFER_ID'], row['APC_OPERATION'])
                if key not in pivot_data:
                    pivot_data[key] = {'WAFER_ID': row['WAFER_ID'], 'APC_OPERATION': row['APC_OPERATION']}
                
                # Handle OPERATION attribute name conflict
                attr_name = row['ATTRIBUTE_NAME']
                if attr_name == 'OPERATION':
                    attr_name = 'OPERATION_ATTR'
                
                apc_attr_name = f"APC_{attr_name}"
                pivot_data[key][apc_attr_name] = row['ATTRIBUTE_VALUE']
            
            df_manual_pivot = pd.DataFrame(list(pivot_data.values()))
            logger.info(f"Manual pivot successful: {df_manual_pivot.shape}")
            
            return df_manual_pivot, {}
            
        except Exception as e2:
            logger.error(f"Manual pivot also failed: {str(e2)}")
            raise e

def main_cascading_area_priority_final(input_file=None, wafer_manifest_path=None):
    """
    Production version: Cascading area filters with completely fixed pivot handling.
    Parameters
    ----------
    input_file : str or None
        Path to the source sDTT CSV.  When None (standalone / legacy use) the
        hard-coded default path is used so the script continues to work as before.
    wafer_manifest_path : str or None
        Path to the run manifest CSV (current_run_wafers_D1V.csv) written by
        finalize_site_data() in 1278sDTT_D1V_F32.py.  When provided that file
        exists, only the wafers listed in the manifest are re-queried from the
        APC database and the freshly enriched rows are merged back into the
        existing _APC.csv (incremental mode).  When None or file not found,
        falls back to full-file mode (all wafers queried).
    """
    # Configuration
    if input_file is None:
        input_file = r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01\debug\1278sDTT_HCCD_D1V.csv"
    database_connection = 'D1D_PROD_XEUS_LOCAL'
    wafer_chunk_size = 80  # Optimized chunk size
    
    start_time = datetime.now()
    
    # Read input file
    logger.info(f"Reading input file: {input_file}")
    df_input = pd.read_csv(input_file)
    logger.info(f"Input data loaded: {len(df_input)} rows")

    # ── Determine query subset based on run manifest (incremental mode) ──────
    # If a manifest CSV is provided and found on disk, only query APC for the
    # wafers processed in the current pipeline run.  The existing _APC.csv is
    # loaded, rows for those wafers are dropped (so they can be re-fetched),
    # and the fresh results are merged back in after querying.  This avoids
    # re-querying 120-day worth of wafers every nightly run and eliminates the
    # OOM risk on the priority-dedup step for large DataFrames.
    df_existing_apc = pd.DataFrame()
    if wafer_manifest_path is not None and Path(wafer_manifest_path).exists():
        logger.info(f"Incremental mode — run manifest: {wafer_manifest_path}")
        _manifest_df = pd.read_csv(wafer_manifest_path)
        _manifest_wafer_ids = set(_manifest_df['WAFER_ID'].astype(str).unique())
        logger.info(f"Manifest: {len(_manifest_wafer_ids)} unique wafer IDs from current run")
        df_query = df_input[df_input['WAFER_ID'].astype(str).isin(_manifest_wafer_ids)].copy()
        logger.info(f"Filtered to {len(df_query)} rows for APC querying "
                    f"({len(df_input)} total in source CSV)")
        # Load existing APC-enriched CSV and drop rows that will be re-fetched
        _existing_apc_path = (Path(input_file).parent /
                              (Path(input_file).stem + '_APC' + Path(input_file).suffix))
        if _existing_apc_path.exists():
            logger.info(f"Loading existing APC CSV for incremental update: {_existing_apc_path}")
            df_existing_apc = pd.read_csv(_existing_apc_path)
            _before_drop = len(df_existing_apc)
            df_existing_apc = df_existing_apc[
                ~df_existing_apc['WAFER_ID'].astype(str).isin(_manifest_wafer_ids)
            ].reset_index(drop=True)
            logger.info(f"Retained {len(df_existing_apc)} existing APC rows "
                        f"(dropped {_before_drop - len(df_existing_apc)} for re-fetch)")
        else:
            logger.info("No existing APC CSV found — will build from scratch for current-run rows")
    elif wafer_manifest_path is not None:
        logger.warning(f"Manifest file not found: {wafer_manifest_path} — "
                       "falling back to full-file mode")
        df_query = df_input
    else:
        logger.info("Full-file mode (no manifest provided) — querying all wafers")
        df_query = df_input

    # Get unique wafer-operation combinations (from query subset only)
    unique_combos = df_query[['WAFER_ID', 'WEC_OPERATION']].drop_duplicates()
    logger.info(f"Unique wafer-operation combinations to query: {len(unique_combos)}")

    # Connect to database
    logger.info("Connecting to database...")
    conn = PyUber.connect(database_connection)

    try:
        # Process operations with cascading area filters
        operations = unique_combos['WEC_OPERATION'].unique()
        logger.info(f"Processing {len(operations)} operations with cascading area filters")
        logger.info(f"Area priority: AMECT_ICCR2 → 8AMEUBE → no area filter")
        logger.info(f"APC system priority: AEPCMC → AEPC2")
        logger.info(f"All APC columns will be prefixed with 'APC_'")

        all_results = []
        total_chunks = 0

        # Global statistics tracking
        global_area_stats = {
            'AEPCMC': {'AMECT_ICCR2': 0, '8AMEUBE': 0, 'no_area_filter': 0, 'failed': 0},
            'AEPC2': {'AMECT_ICCR2': 0, '8AMEUBE': 0, 'no_area_filter': 0, 'failed': 0}
        }

        # Process each operation separately
        for operation in operations:
            operation_wafers = unique_combos[unique_combos['WEC_OPERATION'] == operation]['WAFER_ID'].unique()
            operation_str = f"'{operation}'"

            # Create chunks for this operation
            wafer_chunks = [operation_wafers[i:i + wafer_chunk_size]
                           for i in range(0, len(operation_wafers), wafer_chunk_size)]

            logger.info(f"Operation {operation}: {len(operation_wafers)} wafers in {len(wafer_chunks)} chunks")
            total_chunks += len(wafer_chunks)

            # Process chunks with cascading approach
            for i, wafer_chunk in enumerate(wafer_chunks):
                chunk_id = f"{operation}_chunk_{i+1}"

                chunk_start = datetime.now()
                df_chunk, chunk_area_stats = process_chunk_cascading_priority(
                    wafer_chunk, operation_str, conn, chunk_id
                )
                chunk_time = datetime.now() - chunk_start

                logger.info(f"  {chunk_id} completed in {chunk_time.total_seconds():.2f} seconds")

                if len(df_chunk) > 0:
                    all_results.append(df_chunk)

                # Accumulate statistics
                for system in ['AEPCMC', 'AEPC2']:
                    for area, count in chunk_area_stats.get(system, {}).items():
                        global_area_stats[system][area] += count

                # Memory management
                if (i + 1) % 10 == 0:
                    gc.collect()

    finally:
        # Always close explicitly and remove the Python reference so that
        # Python.NET’s CLRObject finalizer removes the GC handle from its table
        # before Python.Runtime.Shutdown() / NullGCHandles() fires.
        # Without del + gc.collect(), the wrapper stays alive on the heap and
        # NullGCHandles() will dereference a freed .NET object → AccessViolationException.
        try:
            conn.close()
            logger.info("Database connection closed.")
        except Exception as _ce:
            logger.warning(f"Error closing DB connection: {_ce}")
        del conn
        gc.collect()

    logger.info(f"Processed {total_chunks} total chunks.")
    
    # Show global area usage statistics
    logger.info("\n" + "="*60)
    logger.info("GLOBAL AREA USAGE STATISTICS")
    logger.info("="*60)
    for system in ['AEPCMC', 'AEPC2']:
        logger.info(f"{system}:")
        for area, count in global_area_stats[system].items():
            if count > 0:
                logger.info(f"  {area}: {count:,} records")
    logger.info("="*60)
    
    # Process results
    if all_results:
        logger.info("Combining all results...")
        df_apc_combined = pd.concat(all_results, ignore_index=True)
        logger.info(f"Combined APC data: {len(df_apc_combined)} total records")
        
        # Show breakdown by APC system and area
        if len(df_apc_combined) > 0:
            # Add area information to the dataframe for analysis
            area_data = df_apc_combined[df_apc_combined['ATTRIBUTE_NAME'] == 'AREA']
            if len(area_data) > 0:
                area_breakdown = area_data['ATTRIBUTE_VALUE'].value_counts()
                logger.info("Final AREA breakdown in results:")
                for area, count in area_breakdown.items():
                    logger.info(f"  AREA '{area}': {count:,} records")
            
            apc_system_breakdown = df_apc_combined['APC_OBJECT_NAME'].str.extract(r'(AEPCMC|AEPC2)')[0].value_counts()
            logger.info("APC System breakdown:")
            for system, count in apc_system_breakdown.items():
                logger.info(f"  {system}: {count:,} records")
            
            # Show LAMBDA attribute breakdown
            lambda_attrs = df_apc_combined[df_apc_combined['ATTRIBUTE_NAME'].str.contains('LAMBDA', na=False)]
            if len(lambda_attrs) > 0:
                lambda_breakdown = lambda_attrs['ATTRIBUTE_NAME'].value_counts()
                logger.info(f"\nLAMBDA attributes found ({len(lambda_attrs)} total records):")
                for attr, count in lambda_breakdown.items():
                    logger.info(f"  {attr}: {count} records")
            
            # Targeted diagnostics for commonly-null columns
            watch_attrs = []  # removed: METROAVGLOT, METROAVG_CHBR, LAMBDA_PART_USED, LAMBDA_TOOL_USED (no longer queried)
            logger.info("\n--- PRE-DEDUP VALUE AUDIT (watch attributes) ---")
            for attr in watch_attrs:
                attr_rows = df_apc_combined[df_apc_combined['ATTRIBUTE_NAME'] == attr]
                null_mask = attr_rows['ATTRIBUTE_VALUE'].apply(
                    lambda x: x is None or str(x).strip() in ('', '[NULL]', 'nan', 'None')
                )
                non_null_count = (~null_mask).sum()
                null_count = null_mask.sum()
                logger.info(f"  {attr}: {non_null_count} non-null, {null_count} null/[NULL] rows in raw data")
                if non_null_count > 0:
                    sample = attr_rows[~null_mask][['CHANGE_TYPE', 'APC_OBJECT_NAME', 'ATTRIBUTE_VALUE']].head(3)
                    for _, sr in sample.iterrows():
                        logger.info(f"    sample → CHANGE_TYPE={sr['CHANGE_TYPE']}  OBJ={sr['APC_OBJECT_NAME']}  VAL={sr['ATTRIBUTE_VALUE']}")
                else:
                    logger.warning(f"  *** {attr} has NO non-null values at all in raw data — data may not exist for these wafers/operation ***")
            logger.info("--- END PRE-DEDUP VALUE AUDIT ---\n")
        
        del all_results
        gc.collect()
        
        # ── Build source-data SUBENTITY lookup (wafer+operation → SUBENTITY) ─────
        # Used in dedup to prefer the APC record whose reported SUBENTITY matches
        # the chamber the wafer actually ran on (fixes dual-chamber ICCR2 mismatches).
        src_sub_df = (
            df_query[['WAFER_ID', 'WEC_OPERATION', 'SUBENTITY']]
            .drop_duplicates(subset=['WAFER_ID', 'WEC_OPERATION'])
            .rename(columns={'WEC_OPERATION': 'APC_OPERATION', 'SUBENTITY': 'SRC_SUBENTITY'})
        )
        src_sub_df['WAFER_ID']     = src_sub_df['WAFER_ID'].astype(str)
        src_sub_df['APC_OPERATION'] = src_sub_df['APC_OPERATION'].astype(str)
        
        # ── Attach per-job SUBENTITY value to every row in df_apc_combined ───────
        # APC_DATA_ID uniquely identifies each APC job record in P_APC_RUNJOB_HIST.
        # For ICCR2 dual-chamber lots, two sibling records share the same WAFER_ID,
        # APC_OPERATION, TXN_DATE, CHANGE_TYPE, and APC_OBJECT_NAME, so only
        # APC_DATA_ID can tell them apart.  Each sibling has its own SUBENTITY
        # attribute (e.g. PM1 vs PM2) and its own B_TOOL value.
        job_sub = (
            df_apc_combined[df_apc_combined['ATTRIBUTE_NAME'] == 'SUBENTITY']
            .drop_duplicates(subset=['APC_DATA_ID'])
            [['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID', 'ATTRIBUTE_VALUE']]
            .rename(columns={'ATTRIBUTE_VALUE': 'JOB_SUBENTITY'})
        )
        df_apc_combined = df_apc_combined.merge(job_sub, on=['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID'], how='left')
        
        # Join source SUBENTITY onto combined APC data
        df_apc_combined['WAFER_ID']     = df_apc_combined['WAFER_ID'].astype(str)
        df_apc_combined['APC_OPERATION'] = df_apc_combined['APC_OPERATION'].astype(str)
        df_apc_combined = df_apc_combined.merge(src_sub_df, on=['WAFER_ID', 'APC_OPERATION'], how='left')
        
        # Score: 0=exact chamber match (best), 1=parent-only (multi-chamber, neutral),
        #        2=wrong specific chamber (worst — but still has non-null values)
        job_sub_str = df_apc_combined['JOB_SUBENTITY'].astype(str)
        src_sub_str = df_apc_combined['SRC_SUBENTITY'].astype(str)
        df_apc_combined['SUBENTITY_MATCH_PRIORITY'] = 2
        # parent-only (no _PM suffix) → neutral, let other priorities decide
        df_apc_combined.loc[~job_sub_str.str.contains('_PM', case=False, na=True), 'SUBENTITY_MATCH_PRIORITY'] = 1
        # exact match → highest priority
        df_apc_combined.loc[job_sub_str == src_sub_str, 'SUBENTITY_MATCH_PRIORITY'] = 0
        
        match_exact   = (df_apc_combined['SUBENTITY_MATCH_PRIORITY'] == 0).sum()
        match_parent  = (df_apc_combined['SUBENTITY_MATCH_PRIORITY'] == 1).sum()
        match_wrong   = (df_apc_combined['SUBENTITY_MATCH_PRIORITY'] == 2).sum()
        logger.info(f"SUBENTITY match scores — exact: {match_exact:,}  parent-only: {match_parent:,}  wrong chamber: {match_wrong:,}")
        
        # Remove duplicates with enhanced priority handling
        # Priority order: non-null > exact SUBENTITY match > AEPCMC > AEPC2 > AMECT_ICCR2 > 8AMEUBE
        # SUBENTITY_MATCH_PRIORITY resolves dual-chamber lots: prefers the APC record
        # whose reported SUBENTITY matches the source wafer's actual process chamber.
        logger.info("Removing duplicates with priority: non-null > subentity match > AEPCMC > AEPC2 > AMECT_ICCR2 > 8AMEUBE...")
        
        # Create priority scores
        df_apc_combined['APC_SYSTEM_PRIORITY'] = (
            df_apc_combined['APC_OBJECT_NAME'].str.contains('AEPCMC', na=False)
            .map({True: 1, False: 2})
        )

        # CHANGE_TYPE priority: UPDATEPARAMETERS=1, REQUESTSETTINGS=2
        # When both have a value, UPDATEPARAMETERS wins (post-run actuals preferred)
        change_type_col = 'CHANGE_TYPE' if 'CHANGE_TYPE' in df_apc_combined.columns else None
        if change_type_col:
            df_apc_combined['CHANGE_TYPE_PRIORITY'] = (
                (df_apc_combined['CHANGE_TYPE'] == 'UPDATEPARAMETERS')
                .map({True: 1, False: 2})
            )
        else:
            df_apc_combined['CHANGE_TYPE_PRIORITY'] = 1
        
        # Add area priority: use most-frequent AREA value per wafer-operation to avoid
        # dict-key collision when multiple CHANGE_TYPE records exist for the same job
        area_rows = df_apc_combined[df_apc_combined['ATTRIBUTE_NAME'] == 'AREA']
        if len(area_rows) > 0:
            area_map = (
                area_rows.groupby(['WAFER_ID', 'APC_OPERATION'])['ATTRIBUTE_VALUE']
                .agg(lambda s: s.mode().iloc[0] if len(s) > 0 else 'unknown')
                .to_dict()
            )
        else:
            area_map = {}
        # Vectorized area lookup via merge — avoids row-wise apply on large DataFrames
        if area_map:
            area_df = pd.DataFrame(
                [(k[0], k[1], v) for k, v in area_map.items()],
                columns=['WAFER_ID', 'APC_OPERATION', 'RECORD_AREA']
            )
            df_apc_combined = df_apc_combined.merge(
                area_df, on=['WAFER_ID', 'APC_OPERATION'], how='left'
            )
            df_apc_combined['RECORD_AREA'] = df_apc_combined['RECORD_AREA'].fillna('unknown')
        else:
            df_apc_combined['RECORD_AREA'] = 'unknown'
        df_apc_combined['AREA_PRIORITY'] = (
            df_apc_combined['RECORD_AREA']
            .map({'AMECT_ICCR2': 1, '8AMEUBE': 2})
            .fillna(3)
            .astype(int)
        )

        # Null priority: non-null values sort first (0), null/empty/[NULL] sort last (1)
        _null_vals = {'', '[NULL]', 'nan', 'None'}
        df_apc_combined['NULL_PRIORITY'] = (
            df_apc_combined['ATTRIBUTE_VALUE'].isna()
            | df_apc_combined['ATTRIBUTE_VALUE'].astype(str).str.strip().isin(_null_vals)
        ).astype(int)
        
        df_apc_dedup = df_apc_combined.sort_values(
            ['NULL_PRIORITY', 'SUBENTITY_MATCH_PRIORITY', 'APC_SYSTEM_PRIORITY', 'CHANGE_TYPE_PRIORITY', 'AREA_PRIORITY', 'TXN_DATE']
        ).drop_duplicates(
            subset=['WAFER_ID', 'APC_OPERATION', 'ATTRIBUTE_NAME'], 
            keep='first'  # Keep first (non-null, exact-chamber-match, highest priority)
        ).drop(['APC_SYSTEM_PRIORITY', 'AREA_PRIORITY', 'RECORD_AREA', 'CHANGE_TYPE_PRIORITY',
                'NULL_PRIORITY', 'SUBENTITY_MATCH_PRIORITY', 'JOB_SUBENTITY', 'SRC_SUBENTITY',
                'APC_DATA_ID'], axis=1)
        
        logger.info(f"After deduplication: {len(df_apc_dedup)} records")
        
        # Post-dedup audit: confirm watch columns survived with non-null values
        watch_attrs = []  # removed: METROAVGLOT, METROAVG_CHBR, LAMBDA_TOOL_USED (no longer queried)
        logger.info("--- POST-DEDUP VALUE AUDIT ---")
        for attr in watch_attrs:
            attr_rows = df_apc_dedup[df_apc_dedup['ATTRIBUTE_NAME'] == attr]
            null_mask = attr_rows['ATTRIBUTE_VALUE'].apply(
                lambda x: x is None or str(x).strip() in ('', '[NULL]', 'nan', 'None')
            )
            non_null_count = (~null_mask).sum()
            null_count = null_mask.sum()
            if non_null_count > 0:
                logger.info(f"  {attr}: {non_null_count} non-null, {null_count} null rows after dedup ✓")
            else:
                logger.warning(f"  {attr}: 0 non-null rows after dedup — will appear as [NULL] in output")
        
        del df_apc_combined
        gc.collect()
        
        # Safe pivot with enhanced error handling
        df_apc_renamed, column_mapping = safe_pivot_with_prefix_fixed(df_apc_dedup)
        
        del df_apc_dedup
        gc.collect()
        
        # Merge APC data with the queried rows (current-run subset in incremental mode)
        logger.info("Merging APC data with queried rows...")

        df_query['WEC_OPERATION'] = df_query['WEC_OPERATION'].astype(str)
        df_apc_renamed['APC_OPERATION'] = df_apc_renamed['APC_OPERATION'].astype(str)
        df_query['WAFER_ID'] = df_query['WAFER_ID'].astype(str)
        df_apc_renamed['WAFER_ID'] = df_apc_renamed['WAFER_ID'].astype(str)

        df_final = df_query.merge(
            df_apc_renamed,
            left_on=['WAFER_ID', 'WEC_OPERATION'],
            right_on=['WAFER_ID', 'APC_OPERATION'],
            how='left'
        )
        
        # Clean up the merge - remove APC_OPERATION column
        if 'APC_OPERATION' in df_final.columns:
            df_final = df_final.drop('APC_OPERATION', axis=1)
        
        # Apply UBE subentity extraction for 8AMEUBE rows
        df_final = apply_ube_subentity_extraction(df_final)

        # Coerce known-numeric APC columns to float so JMP opens them as Numeric/Continuous.
        # DB returns '[NULL]' as a literal string; any residual packed strings also become NaN.
        numeric_apc_cols = [
            'APC_B_TOOL', 'APC_B_TOOL_RS', 'APC_CALCULATED_SETTING', 'APC_SETTING_USED',
            'APC_M_ETCHRATE', 'APC_OPENRUNS', 'APC_OPENRUNS_PART', 'APC_FB_SUC',
            'APC_LAMBDA_DRIFT', 'APC_LAMBDA_TOOL',
            'APC_LAMBDA_POSTPM', 'APC_LAMBDA_POSTPM_TOOL',
        ]
        for col in numeric_apc_cols:
            if col in df_final.columns:
                before = df_final[col].notna().sum()
                df_final[col] = pd.to_numeric(df_final[col], errors='coerce')
                after = df_final[col].notna().sum()
                dropped = before - after
                if dropped > 0:
                    logger.info(f"Numeric coercion: {col} — {dropped} non-numeric value(s) set to NaN")

        # Reorder columns so preferred/APC columns appear near the front
        # (APC columns were appended at the right by the merge; fix that now).
        df_final = _reorder_columns_apc(df_final)
        logger.info("Column order applied: preferred APC columns moved to front")

        # ── Incremental mode: prepend retained existing APC rows ──────────────
        # df_existing_apc holds the rows from the old _APC.csv that were NOT in
        # the run manifest (i.e. older data already enriched in a prior run).
        # Combine old retained rows with newly enriched current-run rows, then
        # re-sort so DATA_COLLECTION_TIME is still descending across the full file.
        if not df_existing_apc.empty:
            n_new = len(df_final)
            df_final = pd.concat([df_existing_apc, df_final], ignore_index=True)
            if 'DATA_COLLECTION_TIME' in df_final.columns:
                df_final['DATA_COLLECTION_TIME'] = pd.to_datetime(
                    df_final['DATA_COLLECTION_TIME'],
                    format='mixed', dayfirst=False, errors='coerce'
                )
                df_final.sort_values('DATA_COLLECTION_TIME', ascending=False, inplace=True)
                df_final.reset_index(drop=True, inplace=True)
            logger.info(f"Incremental combine: {len(df_existing_apc)} retained rows + "
                        f"{n_new} newly enriched rows = {len(df_final)} total")
            del df_existing_apc
            gc.collect()

        # Save results to a stable fixed filename so JMP and downstream
        # consumers always have a predictable target.  The base SPC/WEC CSV
        # is preserved; the enriched file gets an _APC suffix.
        # e.g. integrated_output\1278sDTT_HCCD_D1V.csv
        #   → integrated_output\1278sDTT_HCCD_D1V_APC.csv
        input_path = Path(input_file)
        output_file = input_path.parent / (input_path.stem + '_APC' + input_path.suffix)

        logger.info(f"Saving results to: {output_file}")
        df_final.to_csv(output_file, index=False)
        
        end_time = datetime.now()
        total_time = end_time - start_time
        
        # Calculate coverage
        apc_columns_check = [col for col in df_final.columns if col.startswith('APC_')]
        
        if apc_columns_check:
            records_with_apc = df_final[apc_columns_check].dropna(how='all')
            apc_coverage = len(records_with_apc) / len(df_final) * 100
            
            # Check LAMBDA coverage specifically
            lambda_columns_in_final = [col for col in df_final.columns if 'APC_LAMBDA' in col]
            if lambda_columns_in_final:
                records_with_lambda = df_final[lambda_columns_in_final].dropna(how='all')
                lambda_coverage = len(records_with_lambda) / len(df_final) * 100
            else:
                lambda_coverage = 0
                
            # Check area coverage
            area_column = 'APC_AREA'
            if area_column in df_final.columns:
                area_coverage = df_final[area_column].value_counts()
                logger.info("Final AREA coverage in output:")
                for area, count in area_coverage.items():
                    pct = count / len(df_final) * 100
                    logger.info(f"  {area}: {count} records ({pct:.1f}%)")
        else:
            apc_coverage = 0
            lambda_coverage = 0
        
        # Check coverage by operation
        operations_with_apc = df_final[df_final[apc_columns_check].notna().any(axis=1)]['WEC_OPERATION'].unique()
        
        # Final summary
        logger.info("=" * 80)
        logger.info("CASCADING AREA PRIORITY - FINAL VERSION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Total processing time: {total_time}")
        logger.info(f"Area cascade: AMECT_ICCR2 → 8AMEUBE → no area filter")
        logger.info(f"APC system priority: AEPCMC → AEPC2")
        logger.info(f"Operations processed: {len(operations)}")
        logger.info(f"Operations with APC data: {len(operations_with_apc)}")
        logger.info(f"Missing operations: {set(operations.astype(str)) - set(operations_with_apc.astype(str))}")
        logger.info(f"Total chunks processed: {total_chunks}")
        logger.info(f"Queried records: {len(df_query):,} (of {len(df_input):,} total in source CSV)")
        logger.info(f"Final dataset shape: {df_final.shape}")
        logger.info(f"Overall APC data coverage: {apc_coverage:.1f}%")
        logger.info(f"LAMBDA data coverage: {lambda_coverage:.1f}%")
        logger.info(f"APC columns added: {len(apc_columns_check)}")
        logger.info(f"LAMBDA columns added: {len(lambda_columns_in_final)} — {lambda_columns_in_final}")
        logger.info(f"Output file: {output_file}")
        logger.info("=" * 80)
        
        return df_final
    
    else:
        logger.error("No APC data retrieved!")
        # In incremental mode with no new APC results, the existing _APC.csv is
        # already on disk and untouched — nothing new to save.  Return the
        # retained existing rows (non-empty when in incremental mode) or the
        # full df_input for the caller's information.
        return df_existing_apc if not df_existing_apc.empty else df_input

def run_apc_join(input_csv_path: str, wafer_manifest_path: str = None) -> str:
    """
    Public entry point for calling the APC join from an external script or
    pipeline orchestrator.

    Parameters
    ----------
    input_csv_path : str
        Full path to the sDTT source CSV (e.g. 1278sDTT_HCCD_D1V.csv).
    wafer_manifest_path : str, optional
        Path to the run manifest CSV (current_run_wafers_D1V.csv) written by
        finalize_site_data() in 1278sDTT_D1V_F32.py.  When provided and the file
        exists on disk, only the wafers listed in the manifest are re-queried
        from the APC DB — results are merged back into the existing _APC.csv
        (incremental mode).  When None or the file is absent, falls back to
        full-file mode (all wafers in input_csv_path are queried).  Pass None
        (or omit) when calling from --apc-only standalone mode.

    Returns
    -------
    str
        Full path to the APC-enriched output CSV, or an empty string on failure.
    """
    from pathlib import Path as _Path
    result_df = main_cascading_area_priority_final(
        input_file=input_csv_path,
        wafer_manifest_path=wafer_manifest_path,
    )
    if result_df is None:
        return ''
    # Return the fixed output path (mirrors the formula in main_cascading_area_priority_final)
    p = _Path(input_csv_path)
    return str(p.parent / (p.stem + '_APC' + p.suffix))


if __name__ == "__main__":
    try:
        result = main_cascading_area_priority_final()
        print(f"\nProcessing completed successfully!")
        print(f"Final dataset shape: {result.shape}")
        
        # Show final area and APC system usage
        if 'APC_AREA' in result.columns:
            area_usage = result['APC_AREA'].value_counts()
            print(f"\nFinal AREA usage:")
            for area, count in area_usage.items():
                print(f"  {area}: {count} records")
        
        # Show APC columns in final output
        apc_cols = [col for col in result.columns if col.startswith('APC_')]
        lambda_cols = [col for col in apc_cols if 'LAMBDA' in col]
        
        print(f"\nAPC columns added: {len(apc_cols)}")
        print(f"LAMBDA columns added: {len(lambda_cols)}")
        
    except KeyboardInterrupt:
        print("\nProcessing interrupted by user")
    except Exception as e:
        print(f"\nProcessing failed: {str(e)}")
        logger.error(f"Processing failed: {str(e)}")