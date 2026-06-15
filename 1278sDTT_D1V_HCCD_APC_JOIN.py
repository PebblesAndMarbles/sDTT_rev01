import re
import argparse
import pandas as pd
import PyUber
import logging
import warnings
from datetime import datetime
from pathlib import Path
import gc

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Silence SQLAlchemy/PyUber loggers that echo SQL text at INFO level
for _noisy in ('sqlalchemy.engine', 'sqlalchemy.engine.base.Engine',
               'sqlalchemy', 'PyUber', 'pyuber'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Suppress pandas SQLAlchemy connectable warning from pd.read_sql() with PyUber
warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy connectable.*')

_PM_RE = re.compile(r'_PM(\d+)', re.IGNORECASE)

SITE_CONFIG = {
    'D1V': {
        'database_connection': 'D1D_PROD_XEUS_LOCAL',
        'site_literal': 'D1V',
        'apc_systems': ['AEPCMC', 'AEPC2'],
        'area_steps': [
            ('MFGAMECT_FLOW_TEMP', True),
            ('AMECT_ICCR2', True),
            ('8AMEUBE', True),
            ('8AMEUBE_GAS', True),
            ('no_area_filter', False),
        ],
    },
    'F32': {
        'database_connection': 'F32_PROD_XEUS_GAJT',
        'site_literal': 'F32',
        'apc_systems': ['AEPCMC', 'AEPC2'],
        'area_steps': [
            ('MFGAMECT_FLOW_TEMP', True),
            ('AMECT_ICCR2', True),
            ('8AMEUBE', True),
            ('no_area_filter', False),
        ],
    },
}


def _get_site_cfg(site: str) -> dict:
    s = str(site).upper()
    if s not in SITE_CONFIG:
        raise ValueError(f"Unsupported site '{site}'. Supported: {sorted(SITE_CONFIG.keys())}")
    return SITE_CONFIG[s]


def _empty_area_counts(site: str) -> dict:
    area_steps = _get_site_cfg(site)['area_steps']
    counts = {area_name: 0 for area_name, has_area_filter in area_steps}
    counts['failed'] = 0
    return counts

# Value payload extraction helpers for FLOW_TEMP area (matrix/vector scalars)
def _is_null_like(value) -> bool:
    """Check if value is pandas NA or null-like string."""
    if pd.isna(value):
        return True
    null_strs = {'', '[NULL]', 'nan', 'None', 'NULL'}
    return str(value).strip() in null_strs

def _strip_brackets(value):
    """Remove outer brackets and handle null-like values."""
    if _is_null_like(value):
        return None
    text = str(value).strip()
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1].strip()
    return None if text in {'', '[NULL]', 'nan', 'None', 'NULL'} else text

def _extract_first_value(value):
    """Extract scalar a-value from matrix/vector/scalar payload.
    Matrix form: a,b;c,d → pick top-left a
    Vector form: a,b → pick first element
    Scalar form: a → use as-is
    """
    text = _strip_brackets(value)
    if text is None:
        return None
    first_row = text.split(';', 1)[0].strip()
    if ',' in first_row:
        return first_row.split(',', 1)[0].strip()
    return first_row

def _value_shape(value) -> str:
    """Diagnostic: return shape category for logging."""
    text = _strip_brackets(value)
    if text is None:
        return 'null'
    if ';' in text:
        return 'matrix'
    if ',' in text:
        return 'vector'
    return 'scalar'

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
    'APC_B_TOOL', 'APC_B_TOOL_RAW', 'APC_SETTING_USED', 'APC_SETTING_USED_RAW', 'APC_OPENRUNS', 'APC_FB_SUC',
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


def _extract_pm_token(value):
    """Extract PM token (e.g. PM5) from a string value; return None when absent."""
    if pd.isna(value):
        return None
    m = _PM_RE.search(str(value))
    return f"PM{m.group(1)}" if m else None


def _build_source_pm_map(df_query: pd.DataFrame) -> dict:
    """Build {(WAFER_ID, WEC_OPERATION): PMx} map from source SUBENTITY values."""
    if 'SUBENTITY' not in df_query.columns:
        return {}

    key_cols = ['WAFER_ID', 'WEC_OPERATION', 'SUBENTITY']
    src = df_query[key_cols].copy()
    src['WAFER_ID'] = src['WAFER_ID'].astype(str)
    src['WEC_OPERATION'] = src['WEC_OPERATION'].astype(str)
    src['SOURCE_PM'] = src['SUBENTITY'].apply(_extract_pm_token)
    src = src[src['SOURCE_PM'].notna()].copy()
    if src.empty:
        return {}

    # Prefer latest source row when duplicates exist
    if 'DATA_COLLECTION_TIME' in df_query.columns:
        src = df_query[['WAFER_ID', 'WEC_OPERATION', 'SUBENTITY', 'DATA_COLLECTION_TIME']].copy()
        src['WAFER_ID'] = src['WAFER_ID'].astype(str)
        src['WEC_OPERATION'] = src['WEC_OPERATION'].astype(str)
        src['SOURCE_PM'] = src['SUBENTITY'].apply(_extract_pm_token)
        src = src[src['SOURCE_PM'].notna()].copy()
        src['DATA_COLLECTION_TIME'] = pd.to_datetime(src['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')
        src = src.sort_values('DATA_COLLECTION_TIME', ascending=False)

    src = src.drop_duplicates(subset=['WAFER_ID', 'WEC_OPERATION'], keep='first')
    return {(r['WAFER_ID'], r['WEC_OPERATION']): r['SOURCE_PM'] for _, r in src.iterrows()}


def _apply_subentity_pm_alignment(df_apc_combined: pd.DataFrame, df_query: pd.DataFrame) -> pd.DataFrame:
    """Filter APC rows so each wafer keeps only APC_DATA_ID rows whose SUBENTITY PM
    matches the wafer's source SUBENTITY PM for that operation.

    This resolves split-chamber lots where AEPCMC_LOT emits one APC_DATA_ID per PM.
    """
    if df_apc_combined.empty:
        return df_apc_combined

    source_pm_map = _build_source_pm_map(df_query)
    if not source_pm_map:
        logger.info("SUBENTITY PM alignment: no source PM map available — skipping")
        return df_apc_combined

    # Determine APC PM token per APC_DATA_ID from SUBENTITY/SUBENTITIES attributes
    apc_key_rows = df_apc_combined[
        df_apc_combined['ATTRIBUTE_NAME'].isin(['SUBENTITY', 'SUBENTITIES'])
    ][['APC_DATA_ID', 'ATTRIBUTE_NAME', 'ATTRIBUTE_VALUE']].copy()
    if apc_key_rows.empty:
        logger.info("SUBENTITY PM alignment: no SUBENTITY/SUBENTITIES APC attributes found — skipping")
        return df_apc_combined

    # Prefer SUBENTITY over SUBENTITIES when both exist for a given APC_DATA_ID
    apc_key_rows['prio'] = apc_key_rows['ATTRIBUTE_NAME'].map({'SUBENTITY': 0, 'SUBENTITIES': 1}).fillna(9)
    apc_key_rows = apc_key_rows.sort_values(['APC_DATA_ID', 'prio'])
    apc_key_rows['APC_PM'] = apc_key_rows['ATTRIBUTE_VALUE'].apply(_extract_pm_token)
    apc_pm_map = apc_key_rows.drop_duplicates(subset=['APC_DATA_ID'], keep='first').set_index('APC_DATA_ID')['APC_PM'].to_dict()

    aligned = df_apc_combined.copy()
    aligned['WAFER_ID'] = aligned['WAFER_ID'].astype(str)
    aligned['APC_OPERATION'] = aligned['APC_OPERATION'].astype(str)
    aligned['SOURCE_PM'] = [source_pm_map.get((w, o)) for w, o in zip(aligned['WAFER_ID'], aligned['APC_OPERATION'])]
    aligned['APC_PM'] = aligned['APC_DATA_ID'].map(apc_pm_map)

    # Keep row when source PM unknown or APC PM unknown, or PMs match.
    # Drop only definitive mismatches.
    keep_mask = (
        aligned['SOURCE_PM'].isna() |
        aligned['APC_PM'].isna() |
        (aligned['SOURCE_PM'] == aligned['APC_PM'])
    )

    before = len(aligned)
    dropped = int((~keep_mask).sum())
    aligned = aligned[keep_mask].copy()
    logger.info(
        f"SUBENTITY PM alignment: kept {len(aligned)} / {before} APC rows; "
        f"dropped {dropped} source-vs-APC PM mismatches"
    )

    return aligned.drop(columns=['SOURCE_PM', 'APC_PM'])


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


def _get_apc_attribute_list(minimal_mode=False):
    if minimal_mode:
        return [
            'B_TOOL', 'AREA',
        ]

    return [
        'CALCULATED_SETTING', 'B_TOOL', 'B_TOOL_RS', 'FB_SUC', 'LOTID', 'M_ETCHRATE',
        'MACHINE', 'OPENRUNS', 'OPENRUNS_PART',
        'OPERATION', 'PROCESS_OPN', 'PRODGROUP', 'PRODUCT', 'SETTING_USED',
        'SUBENTITIES', 'SUBENTITY', 'AREA',
        'LAMBDA_DRIFT', 'LAMBDA_TOOL',
        'LAMBDA_POSTPM', 'LAMBDA_POSTPM_TOOL',
        'B_PART', 'B_PART_PRIOR', 'B_TOOL_PRIOR', 'LAMBDA_PART',
        'METRO_HILIMIT', 'METRO_LOLIMIT', 'MINWAFERS_4_FULLTUNE', 'MODE',
        'NPI_LAMBDA_PART', 'NPI_TUNE_LOTLIMIT',
        'POSTPM_LAMBDA', 'POSTPM_LAMBDA_CLN_LIMIT', 'POSTPM_LAMBDA_TOOL', 'POSTPM_TUNE_LOTLIMIT',
        'REFERENCE_SETTING',
    ]


def _attribute_in_clause(minimal_mode=False):
    attrs = _get_apc_attribute_list(minimal_mode=minimal_mode)
    return ','.join([f"'{a}'" for a in attrs])


def create_apc_query_with_area(wafer_chunk_str, operation_str, apc_system, target_area, minimal_mode=False, site='D1V'):
    """Create APC query with specific area filter"""
    attr_clause = _attribute_in_clause(minimal_mode=minimal_mode)
    _site_literal = _get_site_cfg(site)['site_literal']
    return f"""
    SELECT '{_site_literal}' AS SITE,
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
      AND d.ATTRIBUTE_NAME IN ({attr_clause})
    """

def create_apc_query_no_area(wafer_chunk_str, operation_str, apc_system, minimal_mode=False, site='D1V'):
    """Create APC query without area filter (last resort)"""
    attr_clause = _attribute_in_clause(minimal_mode=minimal_mode)
    _site_literal = _get_site_cfg(site)['site_literal']
    return f"""
    SELECT '{_site_literal}' AS SITE,
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
            AND d.ATTRIBUTE_NAME IN ({attr_clause})
    """

_APC_QUERY_MAX_RETRIES = 2      # number of retries after the first attempt
_APC_QUERY_RETRY_DELAY = 10     # seconds to wait between retries


def _null_like_str_mask(series: pd.Series) -> pd.Series:
    """Return True for null-like strings used in APC payloads."""
    _null_vals = {'', '[NULL]', 'nan', 'None', 'NULL'}
    return series.isna() | series.astype(str).str.strip().isin(_null_vals)


def _matched_wafers_require_area_and_btool(df_result: pd.DataFrame) -> set:
    """Wafers considered matched only when both AREA and B_TOOL are non-null."""
    if df_result.empty:
        return set()

    _needed = df_result[df_result['ATTRIBUTE_NAME'].isin(['AREA', 'B_TOOL'])].copy()
    if _needed.empty:
        return set()

    _needed = _needed[~_null_like_str_mask(_needed['ATTRIBUTE_VALUE'])]
    if _needed.empty:
        return set()

    _dedup = _needed[['WAFER_ID', 'ATTRIBUTE_NAME']].drop_duplicates()
    _counts = _dedup.groupby('WAFER_ID')['ATTRIBUTE_NAME'].nunique()
    return set(_counts[_counts >= 2].index.astype(str))


def _read_sql_with_retry(query, conn, label):
    """Execute pd.read_sql with retry/backoff on transient DB errors.

    Returns a DataFrame (possibly empty).  Raises on final failure.
    label is used only for log messages (e.g. 'chunk_id apc_system area').
    """
    import time
    last_exc = None
    for attempt in range(1, _APC_QUERY_MAX_RETRIES + 2):  # attempts = retries + 1
        try:
            df = pd.read_sql(query, conn)
            if attempt > 1:
                logger.info(f"  {label}: succeeded on attempt {attempt}")
            return df
        except Exception as exc:
            last_exc = exc
            if attempt <= _APC_QUERY_MAX_RETRIES:
                logger.warning(
                    f"  {label}: attempt {attempt} failed ({exc}). "
                    f"Retrying in {_APC_QUERY_RETRY_DELAY}s..."
                )
                time.sleep(_APC_QUERY_RETRY_DELAY)
            else:
                logger.warning(f"  {label}: all {attempt} attempts failed — {exc}")
    raise last_exc


def try_apc_system_with_area_cascade(
    wafer_list,
    operation_str,
    conn,
    apc_system,
    chunk_id,
    minimal_mode=False,
    require_area_btool_for_match=False,
    require_area_btool_for_flow_temp=True,
    site='D1V',
):
    """Cascade through area tiers for remaining wafers within one APC system."""

    if not wafer_list:
        return pd.DataFrame(), _empty_area_counts(site)

    remaining_wafers = set(str(w) for w in wafer_list)
    results = []
    area_counts = _empty_area_counts(site)

    area_steps = _get_site_cfg(site)['area_steps']

    for area_name, has_area_filter in area_steps:
        if not remaining_wafers:
            break

        wafer_chunk_str = "'" + "','".join(sorted(remaining_wafers)) + "'"
        try:
            if has_area_filter:
                    query = create_apc_query_with_area(
                        wafer_chunk_str,
                        operation_str,
                        apc_system,
                        area_name,
                        minimal_mode=minimal_mode,
                        site=site,
                    )
            else:
                    query = create_apc_query_no_area(
                        wafer_chunk_str,
                        operation_str,
                        apc_system,
                        minimal_mode=minimal_mode,
                        site=site,
                    )

            df_result = _read_sql_with_retry(query, conn, f"{chunk_id} {apc_system} {area_name}")
            if len(df_result) == 0:
                continue

            _strict_this_tier = (
                require_area_btool_for_match or
                (require_area_btool_for_flow_temp and area_name == 'MFGAMECT_FLOW_TEMP')
            )

            if _strict_this_tier:
                # Compute matched wafers FIRST.  Only wafers with both AREA and B_TOOL
                # non-null at this tier qualify.  Rows for unmatched wafers (e.g.,
                # FLOW_TEMP returns AREA but no B_TOOL for MT5H) are discarded here so
                # they cannot contaminate results from the next tier after dedup.
                # Previously, results.append happened before this check, causing
                # cross-tier mixing: AREA from FLOW_TEMP, B_TOOL from AMECT_ICCR2.
                matched_wafers = _matched_wafers_require_area_and_btool(df_result)
                if matched_wafers:
                    df_keep = df_result[df_result['WAFER_ID'].astype(str).isin(matched_wafers)]
                    results.append(df_keep)
                    area_counts[area_name] += len(df_keep)
            else:
                matched_wafers = set(df_result['WAFER_ID'].astype(str).unique())
                results.append(df_result)
                area_counts[area_name] += len(df_result)
            remaining_wafers -= matched_wafers

        except Exception as e:
            logger.warning(f"  {chunk_id} {apc_system} {area_name} failed: {str(e)}")

    area_counts['failed'] = len(remaining_wafers)

    if results:
        return pd.concat(results, ignore_index=True), area_counts
    return pd.DataFrame(), area_counts

def process_chunk_cascading_priority(
    wafer_chunk,
    operation_str,
    conn,
    chunk_id,
    minimal_mode=False,
    require_area_btool_for_match=False,
    require_area_btool_for_flow_temp=True,
    site='D1V',
):
    """Process chunk with cascading area filters and APC system priority"""
    try:
        all_wafers = set(wafer_chunk)
        remaining_wafers = list(all_wafers)
        
        results = []
        area_stats = {
            'AEPCMC': _empty_area_counts(site),
            'AEPC2': _empty_area_counts(site)
        }
        
        # Stage 1: Try AEPCMC with area cascade
        apc_systems = _get_site_cfg(site)['apc_systems']
        primary_system = apc_systems[0]
        secondary_system = apc_systems[1] if len(apc_systems) > 1 else None

        df_aepcmc, aepcmc_area_stats = try_apc_system_with_area_cascade(
            remaining_wafers,
            operation_str,
            conn,
            primary_system,
            chunk_id,
            minimal_mode=minimal_mode,
            require_area_btool_for_match=require_area_btool_for_match,
            require_area_btool_for_flow_temp=require_area_btool_for_flow_temp,
            site=site,
        )

        for area, count in aepcmc_area_stats.items():
            area_stats['AEPCMC'][area] += count
        
        if len(df_aepcmc) > 0:
            results.append(df_aepcmc)
            wafers_with_aepcmc = set(df_aepcmc['WAFER_ID'].unique())
            remaining_wafers = list(all_wafers - wafers_with_aepcmc)
        
        # Stage 2: Try AEPC2 for remaining wafers with area cascade
        df_aepc2 = pd.DataFrame()
        if remaining_wafers and secondary_system is not None:
            df_aepc2, aepc2_area_stats = try_apc_system_with_area_cascade(
                remaining_wafers,
                operation_str,
                conn,
                secondary_system,
                chunk_id,
                minimal_mode=minimal_mode,
                require_area_btool_for_match=require_area_btool_for_match,
                require_area_btool_for_flow_temp=require_area_btool_for_flow_temp,
                site=site,
            )

            for area, count in aepc2_area_stats.items():
                area_stats['AEPC2'][area] += count
            
            if len(df_aepc2) > 0:
                results.append(df_aepc2)
                wafers_with_aepc2 = set(df_aepc2['WAFER_ID'].unique())
                remaining_wafers = list(set(remaining_wafers) - wafers_with_aepc2)
        
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

def main_cascading_area_priority_final(
    input_file=None,
    wafer_manifest_path=None,
    fill_null_col=None,
    apc_debug_minimal=False,
    debug_days=3,
    apc_query_lookback_days=None,
    query_key_manifest_path=None,
    output_mode='full',
    patch_output_path=None,
    query_batch_id=None,
    debug_output_suffix='_APC_DEBUG_MINIMAL',
    require_area_btool_for_match_ops=None,
    require_area_btool_for_flow_temp=True,
    use_subentity_pm_match=False,
    site='D1V',
):
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
    fill_null_col : str or None
        When set, load the existing _APC.csv and only re-query wafers where
        this column is null/NaN/[NULL].  Non-null rows are retained as-is.
        Useful for targeted backfills (e.g. fill_null_col='APC_B_TOOL').
    """
    # Configuration
    output_mode = str(output_mode).strip().lower()
    if output_mode not in {'full', 'patch'}:
        raise ValueError(f"Unsupported output_mode '{output_mode}'. Use 'full' or 'patch'.")

    if input_file is None:
        input_file = r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01\debug\1278sDTT_HCCD_D1V.csv"
    _site_cfg = _get_site_cfg(site)
    database_connection = _site_cfg['database_connection']
    wafer_chunk_size = 80  # Optimized chunk size
    
    start_time = datetime.now()
    

    # Read input file
    logger.info(f"Reading input file: {input_file}")
    df_input = pd.read_csv(input_file, low_memory=False)
    logger.info(f"Input data loaded: {len(df_input)} rows")

    # F32 model constraint: Only enrich rows where MODEL == 'MFGAMECT_FLOW_TEMP'.
    # For other models in F32, APC columns must be present but null/blank.
    is_f32 = site.upper() == 'F32'
    apc_model_constraint = 'MFGAMECT_FLOW_TEMP'
    apc_columns = [
        'APC_B_TOOL', 'APC_SETTING_USED', 'APC_OPENRUNS', 'APC_FB_SUC',
        'APC_AREA', 'APC_PRODGROUP', 'APC_B_TOOL_RS', 'APC_CALCULATED_SETTING',
        'APC_M_ETCHRATE', 'APC_OPENRUNS_PART', 'APC_LAMBDA_DRIFT', 'APC_LAMBDA_TOOL',
        'APC_LAMBDA_POSTPM', 'APC_LAMBDA_POSTPM_TOOL', 'APC_B_PART', 'APC_B_PART_PRIOR',
        'APC_B_TOOL_PRIOR', 'APC_LAMBDA_PART', 'APC_METRO_HILIMIT', 'APC_METRO_LOLIMIT',
        'APC_MINWAFERS_4_FULLTUNE', 'APC_MODE', 'APC_NPI_LAMBDA_PART', 'APC_NPI_TUNE_LOTLIMIT',
        'APC_POSTPM_LAMBDA', 'APC_POSTPM_LAMBDA_CLN_LIMIT', 'APC_POSTPM_LAMBDA_TOOL',
        'APC_POSTPM_TUNE_LOTLIMIT', 'APC_REFERENCE_SETTING',
    ]
    if is_f32 and 'MODEL' in df_input.columns:
        logger.info("F32 mode: Only rows with MODEL == 'MFGAMECT_FLOW_TEMP' will be enriched with APC data. Others will have blank/null APC columns.")

    # ── Determine query subset based on run manifest (incremental mode) ──────
    # If a manifest CSV is provided and found on disk, only query APC for the
    # wafers processed in the current pipeline run.  The existing _APC.csv is
    # loaded, rows for those wafers are dropped (so they can be re-fetched),
    # and the fresh results are merged back in after querying.  This avoids
    # re-querying 120-day worth of wafers every nightly run and eliminates the
    # OOM risk on the priority-dedup step for large DataFrames.
    _null_strings = {'', '[NULL]', 'nan', 'None', 'NULL'}
    _existing_apc_path = (Path(input_file).parent /
                          (Path(input_file).stem + '_APC' + Path(input_file).suffix))

    df_existing_apc = pd.DataFrame()
    if fill_null_col is not None:
        # ── Null-fill mode: only re-query wafers missing a specific APC column ──
        if _existing_apc_path.exists():
            logger.info(f"Null-fill mode — target column: {fill_null_col}")
            df_existing_apc = pd.read_csv(_existing_apc_path)
            if fill_null_col in df_existing_apc.columns:
                _null_mask = (
                    df_existing_apc[fill_null_col].isna() |
                    df_existing_apc[fill_null_col].astype(str).str.strip().isin(_null_strings)
                )
                _null_wafer_ids = set(df_existing_apc.loc[_null_mask, 'WAFER_ID'].astype(str).unique())
                logger.info(f"Null-fill: {len(_null_wafer_ids)} unique WAFER_IDs with null {fill_null_col}")
                df_query = df_input[df_input['WAFER_ID'].astype(str).isin(_null_wafer_ids)].copy()
                logger.info(f"Null-fill: {len(df_query)} source rows to re-query "
                            f"(of {len(df_input)} total)")
                _before = len(df_existing_apc)
                df_existing_apc = df_existing_apc[~_null_mask].reset_index(drop=True)
                logger.info(f"Null-fill: retaining {len(df_existing_apc)} non-null rows, "
                            f"dropping {_before - len(df_existing_apc)} for re-query")
            else:
                logger.warning(f"Null-fill: column '{fill_null_col}' not found in existing APC CSV "
                               "— falling back to full-file mode")
                df_query = df_input
                df_existing_apc = pd.DataFrame()
        else:
            logger.warning(f"Null-fill: no existing APC CSV at {_existing_apc_path} "
                           "— falling back to full-file mode")
            df_query = df_input
    elif query_key_manifest_path is not None and Path(query_key_manifest_path).exists():
        logger.info(f"Key-manifest mode — query keys: {query_key_manifest_path}")
        _key_df = pd.read_csv(query_key_manifest_path)
        if 'WAFER_ID' not in _key_df.columns:
            raise ValueError("query_key_manifest_path is missing required column 'WAFER_ID'")

        _key_df['WAFER_ID'] = _key_df['WAFER_ID'].astype(str)
        if 'WEC_OPERATION' in _key_df.columns and 'WEC_OPERATION' in df_input.columns:
            _key_df['WEC_OPERATION'] = _key_df['WEC_OPERATION'].astype(str)
            _pairs = set(zip(_key_df['WAFER_ID'], _key_df['WEC_OPERATION']))
            df_query = df_input[
                [(str(w), str(o)) in _pairs for w, o in zip(df_input['WAFER_ID'], df_input['WEC_OPERATION'])]
            ].copy()
            logger.info(f"Key manifest pairs: {len(_pairs)}")
        else:
            _wafer_ids = set(_key_df['WAFER_ID'].astype(str).unique())
            df_query = df_input[df_input['WAFER_ID'].astype(str).isin(_wafer_ids)].copy()
            logger.info(f"Key manifest wafers: {len(_wafer_ids)}")

        logger.info(f"Filtered to {len(df_query)} rows for APC querying ({len(df_input)} total in source CSV)")

        if output_mode == 'full' and _existing_apc_path.exists():
            logger.info(f"Loading existing APC CSV for key-manifest full merge: {_existing_apc_path}")
            df_existing_apc = pd.read_csv(_existing_apc_path)
            _before_drop = len(df_existing_apc)
            if 'WEC_OPERATION' in _key_df.columns and 'WEC_OPERATION' in df_existing_apc.columns:
                _pairs = set(zip(_key_df['WAFER_ID'].astype(str), _key_df['WEC_OPERATION'].astype(str)))
                _drop_mask = pd.Series(
                    [(str(w), str(o)) in _pairs for w, o in zip(df_existing_apc['WAFER_ID'], df_existing_apc['WEC_OPERATION'])],
                    index=df_existing_apc.index,
                )
                df_existing_apc = df_existing_apc[~_drop_mask].reset_index(drop=True)
            else:
                _wafer_ids = set(_key_df['WAFER_ID'].astype(str).unique())
                df_existing_apc = df_existing_apc[
                    ~df_existing_apc['WAFER_ID'].astype(str).isin(_wafer_ids)
                ].reset_index(drop=True)
            logger.info(f"Retained {len(df_existing_apc)} existing APC rows (dropped {_before_drop - len(df_existing_apc)} for re-fetch)")
        elif output_mode == 'full':
            logger.info("No existing APC CSV found for key-manifest full merge — output will contain queried rows only")
    elif query_key_manifest_path is not None:
        logger.warning(f"Key manifest file not found: {query_key_manifest_path} — falling back to full-file mode")
        df_query = df_input
    elif wafer_manifest_path is not None and Path(wafer_manifest_path).exists():
        logger.info(f"Incremental mode — run manifest: {wafer_manifest_path}")
        _manifest_df = pd.read_csv(wafer_manifest_path)
        _manifest_wafer_ids = set(_manifest_df['WAFER_ID'].astype(str).unique())
        logger.info(f"Manifest: {len(_manifest_wafer_ids)} unique wafer IDs from current run")
        df_query = df_input[df_input['WAFER_ID'].astype(str).isin(_manifest_wafer_ids)].copy()
        logger.info(f"Filtered to {len(df_query)} rows for APC querying "
                    f"({len(df_input)} total in source CSV)")
        # Load existing APC-enriched CSV and drop rows that will be re-fetched
        if _existing_apc_path.exists():
            logger.info(f"Loading existing APC CSV for incremental update: {_existing_apc_path}")
            df_existing_apc = pd.read_csv(_existing_apc_path)
            _before_drop = len(df_existing_apc)
            # Surgical drop: only remove (WAFER_ID, WEC_OPERATION) pairs that appear in the
            # manifest. This preserves historical operation rows for a wafer when only a
            # newer operation is in the current-run manifest, preventing silent APC data loss
            # when apc_query_lookback_days would exclude those older rows from re-query.
            if 'WEC_OPERATION' in _manifest_df.columns and 'WEC_OPERATION' in df_existing_apc.columns:
                _manifest_pairs = set(
                    zip(_manifest_df['WAFER_ID'].astype(str),
                        _manifest_df['WEC_OPERATION'].astype(str))
                )
                _drop_mask = pd.Series(
                    [(str(w), str(o)) in _manifest_pairs
                     for w, o in zip(df_existing_apc['WAFER_ID'], df_existing_apc['WEC_OPERATION'])],
                    index=df_existing_apc.index,
                )
                df_existing_apc = df_existing_apc[~_drop_mask].reset_index(drop=True)
            else:
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

    if apc_debug_minimal:
        if 'DATA_COLLECTION_TIME' in df_query.columns:
            _before_debug = len(df_query)
            _ts = pd.to_datetime(df_query['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')
            _cutoff = pd.Timestamp.now() - pd.Timedelta(days=debug_days)
            df_query = df_query[_ts >= _cutoff].copy()
            logger.info(
                f"APC debug-minimal mode: filtered source rows to last {debug_days} days "
                f"({_before_debug} -> {len(df_query)})"
            )
        else:
            logger.warning(
                "APC debug-minimal mode requested but DATA_COLLECTION_TIME is missing; "
                "using unfiltered source rows"
            )

    # Optional production query-scope limiter for APC DB pull.
    # This trims only df_query (what we query), while output remains full-file via merge.
    if apc_query_lookback_days is not None:
        if 'DATA_COLLECTION_TIME' in df_query.columns:
            _before_lookback = len(df_query)
            _ts = pd.to_datetime(df_query['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce')
            _cutoff = pd.Timestamp.now() - pd.Timedelta(days=int(apc_query_lookback_days))
            df_query = df_query[_ts >= _cutoff].copy()
            logger.info(
                f"APC query lookback filter: last {int(apc_query_lookback_days)} days "
                f"({_before_lookback} -> {len(df_query)} rows)"
            )
        else:
            logger.warning(
                "APC query lookback filter requested but DATA_COLLECTION_TIME is missing; "
                "using unfiltered query scope"
            )


    # For patch mode, merge only queried rows. For full mode, preserve historical behavior.
    _base_merge_df = df_query if output_mode == 'patch' else df_input

    # For F32, filter to only rows with MODEL == 'MFGAMECT_FLOW_TEMP' for APC enrichment
    if is_f32 and 'MODEL' in _base_merge_df.columns:
        mask_model = _base_merge_df['MODEL'] == apc_model_constraint
        df_input_f32_model = _base_merge_df[mask_model].copy()
        df_input_f32_other = _base_merge_df[~mask_model].copy()
        logger.info(f"F32: {len(df_input_f32_model)} rows with MODEL == '{apc_model_constraint}', {len(df_input_f32_other)} rows with other models.")
    else:
        df_input_f32_model = _base_merge_df
        df_input_f32_other = pd.DataFrame(columns=_base_merge_df.columns)

    # Ensure F32 APC query scope only includes the target model.
    if is_f32 and 'MODEL' in df_query.columns:
        _before_f32_scope = len(df_query)
        df_query = df_query[df_query['MODEL'] == apc_model_constraint].copy()
        logger.info(
            f"F32 query scope: filtered to MODEL == '{apc_model_constraint}' "
            f"({_before_f32_scope} -> {len(df_query)} rows)"
        )
    # Get unique wafer-operation combinations (from query subset only)
    unique_combos = df_query[['WAFER_ID', 'WEC_OPERATION']].drop_duplicates()
    logger.info(f"Unique wafer-operation combinations to query: {len(unique_combos)}")

    # Connect to database
    logger.info(f"Connecting to database for site {site}: {database_connection}")
    conn = PyUber.connect(database_connection)

    _strict_ops = set(str(x) for x in (require_area_btool_for_match_ops or []))
    if require_area_btool_for_flow_temp:
        logger.info(
            "FLOW_TEMP strict tier enabled: wafers only match at MFGAMECT_FLOW_TEMP when AREA and B_TOOL are both non-null"
        )
    if _strict_ops:
        logger.info(
            "Strict match mode enabled for operations (requires non-null AREA and B_TOOL): "
            f"{sorted(_strict_ops)}"
        )

    try:
        # Process operations with cascading area filters
        operations = unique_combos['WEC_OPERATION'].unique()
        logger.info(f"Processing {len(operations)} operations with cascading area filters for site {site}")
        logger.info(f"Area priority: MFGAMECT_FLOW_TEMP → AMECT_ICCR2 → 8AMEUBE → no area filter")
        logger.info(f"APC system priority: AEPCMC → AEPC2")
        logger.info(f"All APC columns will be prefixed with 'APC_'")
        if apc_debug_minimal:
            logger.info("APC debug-minimal mode enabled: querying only AREA and B_TOOL attributes")

        all_results = []
        total_chunks = 0

        # Global statistics tracking
        global_area_stats = {
            'AEPCMC': _empty_area_counts(site),
            'AEPC2': _empty_area_counts(site)
        }

        # Process each operation separately
        for operation in operations:
            operation_wafers = unique_combos[unique_combos['WEC_OPERATION'] == operation]['WAFER_ID'].unique()
            operation_str = f"'{operation}'"
            _strict_this_operation = str(operation) in _strict_ops
            if _strict_this_operation:
                logger.info(
                    f"Operation {operation}: strict match enabled (wafer advances only if AREA and B_TOOL are both non-null)"
                )

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
                    wafer_chunk,
                    operation_str,
                    conn,
                    chunk_id,
                    minimal_mode=apc_debug_minimal,
                    require_area_btool_for_match=_strict_this_operation,
                    require_area_btool_for_flow_temp=require_area_btool_for_flow_temp,
                    site=site,
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

        if use_subentity_pm_match:
            logger.info("Applying SUBENTITY PM alignment for split-chamber lot APC rows...")
            df_apc_combined = _apply_subentity_pm_alignment(df_apc_combined, df_query)

        # ── Deduplicate: keep one row per (WAFER_ID, APC_OPERATION, ATTRIBUTE_NAME) ──
        # Prefer most-recent TXN_DATE so the freshest APC record wins.
        logger.info("Deduplicating APC results...")
        _sort_col = 'TXN_DATE' if 'TXN_DATE' in df_apc_combined.columns else 'OUT_DATE'
        if _sort_col in df_apc_combined.columns:
            df_apc_combined = df_apc_combined.sort_values(_sort_col, ascending=False)
        df_dedup = df_apc_combined.drop_duplicates(
            subset=['WAFER_ID', 'APC_OPERATION', 'ATTRIBUTE_NAME'], keep='first'
        )
        logger.info(f"After dedup: {len(df_dedup)} rows (from {len(df_apc_combined)})")
        del df_apc_combined
        gc.collect()

        # ── Pivot APC attribute rows → wide format with APC_ prefix ──────────
        df_apc_wide, _ = safe_pivot_with_prefix_fixed(df_dedup)
        del df_dedup
        gc.collect()

        # ── Merge APC wide data back into the source rows ─────────────────────
        # For F32 with MODEL column: df_input_f32_model contains only the
        # MFGAMECT_FLOW_TEMP rows; for D1V it equals df_input.
        logger.info("Merging APC data into source rows...")
        # Coerce merge keys to the same type (both → str) to avoid int64/object mismatch
        df_input_f32_model = df_input_f32_model.copy()
        df_input_f32_model['WEC_OPERATION'] = df_input_f32_model['WEC_OPERATION'].astype(str)
        if 'APC_OPERATION' in df_apc_wide.columns:
            df_apc_wide['APC_OPERATION'] = df_apc_wide['APC_OPERATION'].astype(str)
        df_final = pd.merge(
            df_input_f32_model,
            df_apc_wide,
            left_on=['WAFER_ID', 'WEC_OPERATION'],
            right_on=['WAFER_ID', 'APC_OPERATION'],
            how='left',
        )
        if 'APC_OPERATION' in df_final.columns:
            df_final = df_final.drop(columns=['APC_OPERATION'])

        # ── Re-attach retained existing APC rows (incremental / null-fill) ────
        if not df_existing_apc.empty:
            logger.info(f"Appending {len(df_existing_apc)} retained existing APC rows...")
            for col in df_final.columns:
                if col not in df_existing_apc.columns:
                    df_existing_apc[col] = pd.NA
            df_existing_apc = df_existing_apc[df_final.columns]
            df_final = pd.concat([df_existing_apc, df_final], ignore_index=True)
            logger.info(f"Combined total after merge with existing: {len(df_final)} rows")

        # ── Column ordering and UBE sub-chamber extraction ────────────────────
        df_final = _reorder_columns_apc(df_final)
        if 'APC_AREA' in df_final.columns:
            df_final = apply_ube_subentity_extraction(df_final)

        # --- F32 model constraint enforcement ---
        if is_f32 and 'MODEL' in df_input.columns:
            # df_final contains only rows with MODEL == MFGAMECT_FLOW_TEMP (from df_input_f32_model)
            # Now, append the other rows (df_input_f32_other) with blank/null APC columns
            missing_apc_cols = [col for col in apc_columns if col not in df_final.columns]
            for col in missing_apc_cols:
                df_final[col] = None
            # For rows with other models, add all APC columns as nulls
            for col in apc_columns:
                if col not in df_input_f32_other.columns:
                    df_input_f32_other[col] = None
            # Reorder columns to match df_final
            df_input_f32_other = df_input_f32_other[df_final.columns]
            # Concatenate enriched and null-APC rows
            df_final = pd.concat([df_final, df_input_f32_other], ignore_index=True)
            # Restore original row order if possible
            if 'DATA_COLLECTION_TIME' in df_final.columns:
                df_final = df_final.sort_values('DATA_COLLECTION_TIME', ascending=False).reset_index(drop=True)
            logger.info(f"F32: Appended {len(df_input_f32_other)} rows with non-MFGAMECT_FLOW_TEMP model and blank APC columns.")

        # ── Preserve raw vector payloads before scalar extraction ──────────────────
        # FLOW_TEMP area returns B_TOOL, M_ETCHRATE, and SETTING_USED as vector/matrix
        # strings (e.g. "0.0169,0"). Copy to _RAW columns before in-place extraction
        # so the full payload is never lost.
        _raw_preserve_cols = ['APC_B_TOOL', 'APC_M_ETCHRATE', 'APC_SETTING_USED']
        for _rpc in _raw_preserve_cols:
            if _rpc in df_final.columns:
                df_final[f'{_rpc}_RAW'] = df_final[_rpc].copy()

        # ── Extract scalar from vector/matrix payloads (FLOW_TEMP returns "a,b" form) ─
        # Applied on df_final AFTER all concat (fresh + retained existing rows) so that
        # retained rows from prior runs also get cleaned. _extract_first_value is a no-op
        # for plain scalars, so it is safe to apply unconditionally to any numeric col.
        _vector_cols = [
            'APC_B_TOOL', 'APC_B_TOOL_RS', 'APC_B_PART', 'APC_B_PART_PRIOR',
            'APC_B_TOOL_PRIOR', 'APC_CALCULATED_SETTING', 'APC_REFERENCE_SETTING',
            'APC_LAMBDA_DRIFT', 'APC_LAMBDA_TOOL', 'APC_LAMBDA_POSTPM',
            'APC_LAMBDA_POSTPM_TOOL', 'APC_LAMBDA_PART',
        ]
        _cleaned = []
        for _vc in _vector_cols:
            if _vc in df_final.columns:
                df_final[_vc] = df_final[_vc].apply(_extract_first_value)
                _cleaned.append(_vc)
        if _cleaned:
            logger.info(f"Vector/matrix extraction applied to {len(_cleaned)} columns: {_cleaned}")

        # ── Dedup before writing to prevent accumulation in incremental mode ────
        # Keep the most recent record for each (WEC_LAYER, SPC_LOT, WID) tuple
        # based on DATA_COLLECTION_TIME. This prevents duplication from incremental
        # merges over many pipeline runs.
        if 'WEC_LAYER' in df_final.columns and 'SPC_LOT' in df_final.columns and 'WID' in df_final.columns:
            dedup_key = ['WEC_LAYER', 'SPC_LOT', 'WID']
            rows_before_dedup = len(df_final)
            
            if 'DATA_COLLECTION_TIME' in df_final.columns:
                # Sort by DATA_COLLECTION_TIME descending (most recent first).
                # Secondary sort: prefer rows WITH APC_AREA filled over null-APC rows when
                # timestamps tie (which happens in incremental mode where the full source
                # LEFT JOIN produces null-APC rows for historical wafers that are also in
                # df_existing_apc with good salvage/prior APC data).
                df_final['DATA_COLLECTION_TIME'] = pd.to_datetime(
                    df_final['DATA_COLLECTION_TIME'],
                    format='mixed', dayfirst=False, errors='coerce'
                )
                df_final['_has_apc'] = df_final['APC_AREA'].notna().astype(int) if 'APC_AREA' in df_final.columns else 0
                df_final = df_final.sort_values(
                    by=['DATA_COLLECTION_TIME', '_has_apc'],
                    ascending=[False, False],
                    kind='mergesort',  # stable sort preserves concat order within equal keys
                )
                df_final = df_final.drop(columns=['_has_apc'])
            
            df_final = df_final.drop_duplicates(subset=dedup_key, keep='first').reset_index(drop=True)
            rows_after_dedup = len(df_final)
            dupes_removed = rows_before_dedup - rows_after_dedup
            
            if dupes_removed > 0:
                logger.info(f"APC dedup before write: removed {dupes_removed} duplicates "
                           f"({rows_before_dedup} -> {rows_after_dedup} rows)")
        else:
            logger.warning("APC dedup skipped: missing one or more columns (WEC_LAYER, SPC_LOT, WID)")

        # Add trace fields in patch mode for deterministic fold-in auditing.
        if output_mode == 'patch':
            _batch = query_batch_id if query_batch_id is not None else datetime.now().strftime('%Y%m%d_%H%M%S')
            df_final['APC_QUERY_MODE'] = 'patch'
            df_final['APC_QUERY_BATCH_ID'] = str(_batch)
            df_final['APC_QUERY_TIMESTAMP'] = datetime.now().isoformat(timespec='seconds')
            df_final['APC_QUERY_SITE'] = str(site).upper()

        # [rest of the unchanged code for saving, logging, and returning df_final]
        input_path = Path(input_file)
        if output_mode == 'patch':
            if patch_output_path is not None:
                output_file = Path(patch_output_path)
            else:
                _batch = query_batch_id if query_batch_id is not None else datetime.now().strftime('%Y%m%d_%H%M%S')
                output_file = input_path.parent / (input_path.stem + f'_APC_PATCH_{_batch}' + input_path.suffix)
        elif apc_debug_minimal:
            output_file = input_path.parent / (input_path.stem + debug_output_suffix + input_path.suffix)
        else:
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

def run_apc_join(
    input_csv_path: str,
    wafer_manifest_path: str = None,
    query_key_manifest_path: str = None,
    output_mode: str = 'full',
    patch_output_path: str = None,
    query_batch_id: str = None,
    fill_null_col: str = None,
    apc_debug_minimal: bool = False,
    debug_days: int = 3,
    apc_query_lookback_days: int = None,
    debug_output_suffix: str = '_APC_DEBUG_MINIMAL',
    require_area_btool_for_match_ops=None,
    require_area_btool_for_flow_temp: bool = True,
    use_subentity_pm_match: bool = False,
    site: str = 'D1V',
) -> str:
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
    fill_null_col : str, optional
        When set, only re-query wafers where this APC column is null in the
        existing _APC.csv (e.g. 'APC_B_TOOL').  Non-null rows are retained.
    query_key_manifest_path : str, optional
        Path to a key manifest CSV for targeted APC re-query. Supports
        required column WAFER_ID and optional column WEC_OPERATION.
    output_mode : str, optional
        Output mode: 'full' (default) writes standard _APC output behavior,
        'patch' writes only queried-key sidecar output for later fold-in.
    patch_output_path : str, optional
        Output path for patch mode. When omitted, a timestamped
        *_APC_PATCH_<batch>.csv is written beside input_csv_path.
    query_batch_id : str, optional
        Optional batch identifier for patch traceability.
    apc_debug_minimal : bool, optional
        When True, run a minimal debug APC pull (AREA + B_TOOL) over recent
        source rows and save to a separate debug APC output file.
    debug_days : int, optional
        Source-row lookback window for debug-minimal mode (default: 3).
    apc_query_lookback_days : int, optional
        Limits APC DB querying to source rows whose DATA_COLLECTION_TIME is
        within the last N days. This trims query scope only; final output
        still includes the full source CSV after merge.
    debug_output_suffix : str, optional
        File suffix used in debug-minimal mode output naming.
    require_area_btool_for_match_ops : list[str] | None, optional
        Optional operation list for strict match retry testing. For these
        operations, a wafer is considered matched at a given area only when
        both AREA and B_TOOL are non-null, otherwise it continues to lower
        area retry tiers.
    require_area_btool_for_flow_temp : bool, optional
        When True (default), applies strict AREA+B_TOOL match qualification
        specifically at the MFGAMECT_FLOW_TEMP tier for all operations.
    use_subentity_pm_match : bool, optional
        When True, aligns APC rows by PM token so each wafer keeps only
        APC_DATA_ID records whose SUBENTITY PM matches source SUBENTITY PM.
        Useful for split-chamber lots where AEPCMC_LOT emits one APC_DATA_ID
        per PM.
    site : str, optional
        Site selector for APC query context (default: 'D1V'). Supported:
        'D1V', 'F32'.

    Returns
    -------
    str
        Full path to the APC-enriched output CSV, or an empty string on failure.
    """
    from pathlib import Path as _Path
    if _Path(input_csv_path).stem.endswith('_APC'):
        raise ValueError(
            f"input_csv_path appears to be an APC output file (stem ends with '_APC'): "
            f"'{input_csv_path}'. Pass the base HCCD CSV instead "
            "(e.g. '1278sDTT_HCCD_D1V.csv', not '1278sDTT_HCCD_D1V_APC.csv')."
        )
    result_df = main_cascading_area_priority_final(
        input_file=input_csv_path,
        wafer_manifest_path=wafer_manifest_path,
        query_key_manifest_path=query_key_manifest_path,
        output_mode=output_mode,
        patch_output_path=patch_output_path,
        query_batch_id=query_batch_id,
        fill_null_col=fill_null_col,
        apc_debug_minimal=apc_debug_minimal,
        debug_days=debug_days,
        apc_query_lookback_days=apc_query_lookback_days,
        debug_output_suffix=debug_output_suffix,
        require_area_btool_for_match_ops=require_area_btool_for_match_ops,
        require_area_btool_for_flow_temp=require_area_btool_for_flow_temp,
        use_subentity_pm_match=use_subentity_pm_match,
        site=site,
    )
    if result_df is None:
        return ''
    # Return the output path (mirrors the formula in main_cascading_area_priority_final)
    p = _Path(input_csv_path)
    if str(output_mode).strip().lower() == 'patch':
        if patch_output_path:
            return str(_Path(patch_output_path))
        _batch = query_batch_id if query_batch_id is not None else datetime.now().strftime('%Y%m%d_%H%M%S')
        return str(p.parent / (p.stem + f'_APC_PATCH_{_batch}' + p.suffix))
    if apc_debug_minimal:
        return str(p.parent / (p.stem + debug_output_suffix + p.suffix))
    return str(p.parent / (p.stem + '_APC' + p.suffix))


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description='sDTT APC join runner')
        parser.add_argument('--input-file', default=None, help='Source CSV path (e.g. 1278sDTT_HCCD_F32.csv)')
        parser.add_argument('--site', default='D1V', choices=['D1V', 'F32'], help='APC site context')
        parser.add_argument('--minimal', action='store_true', dest='apc_debug_minimal',
                            help='Run minimal APC query mode (AREA + B_TOOL only)')
        parser.add_argument('--days', type=int, default=3, dest='debug_days',
                            help='Lookback window (days) used by minimal mode filter')
        parser.add_argument('--query-lookback-days', type=int, default=None, dest='apc_query_lookback_days',
                    help='Optional: limit APC DB query scope to recent source rows by DATA_COLLECTION_TIME')
        parser.add_argument('--query-key-manifest', default=None, dest='query_key_manifest_path',
                help='Optional key-manifest CSV path (WAFER_ID and optional WEC_OPERATION) for targeted APC re-query')
        parser.add_argument('--output-mode', default='full', choices=['full', 'patch'], dest='output_mode',
                help="Output mode: 'full' (standard) or 'patch' (sidecar rows for later merge)")
        parser.add_argument('--patch-output-path', default=None, dest='patch_output_path',
                help='Optional explicit output path for patch mode')
        parser.add_argument('--query-batch-id', default=None, dest='query_batch_id',
                help='Optional batch id used in patch output naming and trace columns')
        parser.add_argument('--output-suffix', default='_APC_DEBUG_MINIMAL', dest='debug_output_suffix',
                            help='Output suffix for minimal/debug modes')
        parser.add_argument('--use-subentity-pm-match', action='store_true',
                    dest='use_subentity_pm_match',
                    help='Align APC rows by PM token (source SUBENTITY vs APC SUBENTITY)')

        args = parser.parse_args()

        result = main_cascading_area_priority_final(
            input_file=args.input_file,
            query_key_manifest_path=args.query_key_manifest_path,
            output_mode=args.output_mode,
            patch_output_path=args.patch_output_path,
            query_batch_id=args.query_batch_id,
            apc_debug_minimal=args.apc_debug_minimal,
            debug_days=args.debug_days,
            apc_query_lookback_days=args.apc_query_lookback_days,
            debug_output_suffix=args.debug_output_suffix,
            use_subentity_pm_match=args.use_subentity_pm_match,
            site=args.site,
        )
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