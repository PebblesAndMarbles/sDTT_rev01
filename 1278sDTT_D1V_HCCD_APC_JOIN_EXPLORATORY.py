import argparse
import gc
import importlib.util
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import PyUber

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TARGET_SUBENTITIES = [
    'AME417_PM1',
    'AME417_PM2',
    'AME417_PM3',
    'AME417_PM4',
    'AME417_PM5',
    'AME417_PM6',
]

DEFAULT_INPUT = r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_D1V.csv"
DEFAULT_OUTPUT_DIR = r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01\integrated_output"
DEFAULT_DATABASE = 'D1D_PROD_XEUS_LOCAL'
DEFAULT_TARGET_AREA = 'MFGAMECT_FLOW_TEMP'
DEFAULT_CHUNK_SIZE = 80
DEFAULT_SAMPLE_DAYS = 10

# Core APC attributes required for stable join/dedup behavior in this exploratory flow.
CORE_APC_ATTRIBUTES = [
    'CALCULATED_SETTING', 'B_TOOL', 'B_TOOL_RS', 'FB_SUC', 'LOTID', 'M_ETCHRATE',
    'MACHINE', 'OPENRUNS', 'OPENRUNS_PART',
    'OPERATION', 'PROCESS_OPN', 'PRODGROUP', 'PRODUCT', 'SETTING_USED',
    'SUBENTITIES', 'SUBENTITY', 'AREA',
    'LAMBDA_DRIFT', 'LAMBDA_TOOL', 'LAMBDA_POSTPM', 'LAMBDA_POSTPM_TOOL',
]

# Exact-match fields confirmed available in DB for AME417 PM1..PM6 strict-area sample.
EXACT_MATCH_REQUESTED_ATTRIBUTES = [
    'MODE', 'REFERENCE_SETTING', 'LAMBDA_PART', 'B_PART', 'B_PART_PRIOR',
    'B_TOOL_PRIOR',
    'METRO_LOLIMIT', 'METRO_HILIMIT', 'MINWAFERS_4_FULLTUNE',
    'NPI_LAMBDA_PART', 'NPI_TUNE_LOTLIMIT',
    'POSTPM_LAMBDA', 'POSTPM_LAMBDA_CLN_LIMIT', 'POSTPM_LAMBDA_TOOL', 'POSTPM_TUNE_LOTLIMIT',
]

APC_QUERY_ATTRIBUTES = sorted(set(CORE_APC_ATTRIBUTES + EXACT_MATCH_REQUESTED_ATTRIBUTES))


def load_base_apc_module(repo_root: Path):
    """Load existing APC join module by file path so we can reuse proven helpers."""
    module_path = repo_root / '1278sDTT_D1V_HCCD_APC_JOIN.py'
    spec = importlib.util.spec_from_file_location('apc_base', str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load module spec for {module_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def filter_source_rows(df: pd.DataFrame, sample_days: int) -> pd.DataFrame:
    """Restrict source rows to requested chambers and recent collection time window."""
    required_cols = {'WAFER_ID', 'WEC_OPERATION', 'SUBENTITY'}
    missing = required_cols - set(df.columns)
    if missing:
        raise KeyError(f'Missing required source columns: {sorted(missing)}')

    before = len(df)
    df = df[df['SUBENTITY'].isin(TARGET_SUBENTITIES)].copy()
    logger.info('SUBENTITY filter: %s -> %s rows', before, len(df))

    if sample_days > 0 and 'DATA_COLLECTION_TIME' in df.columns:
        ts = pd.to_datetime(df['DATA_COLLECTION_TIME'], format='mixed', errors='coerce')
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=sample_days)
        before_time = len(df)
        df = df[ts >= cutoff].copy()
        logger.info('Time filter (last %s days): %s -> %s rows', sample_days, before_time, len(df))

    df['WAFER_ID'] = df['WAFER_ID'].astype(str)
    df['WEC_OPERATION'] = df['WEC_OPERATION'].astype(str)
    return df


def query_apc_for_system_and_area(conn, wafer_list, operation_str, apc_system, target_area):
    """Query APC for one system with strict area constraint only."""
    if not wafer_list:
        return pd.DataFrame()

    wafer_chunk_str = "'" + "','".join(wafer_list) + "'"
    attribute_name_str = "'" + "','".join(APC_QUERY_ATTRIBUTES) + "'"
    query = f"""
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
      AND d.ATTRIBUTE_NAME IN ({attribute_name_str})
    """
    return pd.read_sql(query, conn)


def query_apc_strict_area(base_module, df_filtered: pd.DataFrame, database_connection: str, target_area: str, chunk_size: int):
    """Query APC data with strict AREA filter and APC system priority AEPCMC -> AEPC2."""
    unique_combos = df_filtered[['WAFER_ID', 'WEC_OPERATION']].drop_duplicates()
    operations = unique_combos['WEC_OPERATION'].unique()
    logger.info('Unique wafer-operation pairs: %s across %s operations', len(unique_combos), len(operations))

    stats = {
        'operations': len(operations),
        'chunks': 0,
        'aepcmc_rows': 0,
        'aepc2_rows': 0,
        'remaining_unmatched_wafers': 0,
    }
    all_results = []

    conn = PyUber.connect(database_connection)
    try:
        for operation in operations:
            op_wafers = unique_combos[unique_combos['WEC_OPERATION'] == operation]['WAFER_ID'].unique()
            op_wafers = [str(w) for w in op_wafers]
            operation_str = f"'{operation}'"
            wafer_chunks = [op_wafers[i:i + chunk_size] for i in range(0, len(op_wafers), chunk_size)]

            logger.info('Operation %s: %s wafers in %s chunks', operation, len(op_wafers), len(wafer_chunks))
            stats['chunks'] += len(wafer_chunks)

            for idx, wafer_chunk in enumerate(wafer_chunks, start=1):
                chunk_id = f'{operation}_chunk_{idx}'
                all_wafers = set(wafer_chunk)
                remaining = list(all_wafers)

                df_aepcmc = query_apc_for_system_and_area(
                    conn=conn,
                    wafer_list=remaining,
                    operation_str=operation_str,
                    apc_system='AEPCMC',
                    target_area=target_area,
                )
                if not df_aepcmc.empty:
                    all_results.append(df_aepcmc)
                    stats['aepcmc_rows'] += len(df_aepcmc)
                    matched = set(df_aepcmc['WAFER_ID'].astype(str).unique())
                    remaining = list(all_wafers - matched)

                df_aepc2 = pd.DataFrame()
                if remaining:
                    df_aepc2 = query_apc_for_system_and_area(
                        conn=conn,
                        wafer_list=remaining,
                        operation_str=operation_str,
                        apc_system='AEPC2',
                        target_area=target_area,
                    )
                    if not df_aepc2.empty:
                        all_results.append(df_aepc2)
                        stats['aepc2_rows'] += len(df_aepc2)
                        matched2 = set(df_aepc2['WAFER_ID'].astype(str).unique())
                        remaining = list(set(remaining) - matched2)

                stats['remaining_unmatched_wafers'] += len(remaining)
                logger.info(
                    '%s: AEPCMC rows=%s, AEPC2 rows=%s, unmatched wafers=%s',
                    chunk_id,
                    len(df_aepcmc),
                    len(df_aepc2),
                    len(remaining),
                )

                gc.collect()

    finally:
        try:
            conn.close()
        except Exception as close_err:
            logger.warning('Error closing DB connection: %s', close_err)
        del conn
        gc.collect()

    if all_results:
        df_apc_raw = pd.concat(all_results, ignore_index=True)
    else:
        df_apc_raw = pd.DataFrame(
            columns=[
                'SITE', 'LOT', 'WAFER_ID', 'APC_OPERATION', 'OUT_DATE', 'APC_DATA_ID',
                'TXN_DATE', 'APC_OBJECT_NAME', 'APC_OBJECT_TYPE', 'CHANGE_TYPE',
                'ATTRIBUTE_NAME', 'ATTRIBUTE_VALUE',
            ]
        )

    return df_apc_raw, stats


def dedup_pivot_merge(base_module, df_filtered: pd.DataFrame, df_apc_raw: pd.DataFrame, target_area: str):
    """Apply dedup + pivot logic and merge APC columns back to filtered source rows."""
    if df_apc_raw.empty:
        logger.warning('No APC rows returned. Writing filtered source rows with empty APC columns.')
        out = df_filtered.copy()
        out['APC_MATCHED'] = False
        return out

    src_sub_df = (
        df_filtered[['WAFER_ID', 'WEC_OPERATION', 'SUBENTITY']]
        .drop_duplicates(subset=['WAFER_ID', 'WEC_OPERATION'])
        .rename(columns={'WEC_OPERATION': 'APC_OPERATION', 'SUBENTITY': 'SRC_SUBENTITY'})
    )

    df_apc_raw['WAFER_ID'] = df_apc_raw['WAFER_ID'].astype(str)
    df_apc_raw['APC_OPERATION'] = df_apc_raw['APC_OPERATION'].astype(str)

    job_sub = (
        df_apc_raw[df_apc_raw['ATTRIBUTE_NAME'] == 'SUBENTITY']
        .drop_duplicates(subset=['APC_DATA_ID'])
        [['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID', 'ATTRIBUTE_VALUE']]
        .rename(columns={'ATTRIBUTE_VALUE': 'JOB_SUBENTITY'})
    )

    df_apc = df_apc_raw.merge(job_sub, on=['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID'], how='left')
    df_apc = df_apc.merge(src_sub_df, on=['WAFER_ID', 'APC_OPERATION'], how='left')

    job_sub_str = df_apc['JOB_SUBENTITY'].astype(str)
    src_sub_str = df_apc['SRC_SUBENTITY'].astype(str)

    df_apc['SUBENTITY_MATCH_PRIORITY'] = 2
    df_apc.loc[~job_sub_str.str.contains('_PM', case=False, na=True), 'SUBENTITY_MATCH_PRIORITY'] = 1
    df_apc.loc[job_sub_str == src_sub_str, 'SUBENTITY_MATCH_PRIORITY'] = 0

    df_apc['APC_SYSTEM_PRIORITY'] = (
        df_apc['APC_OBJECT_NAME'].str.contains('AEPCMC', na=False).map({True: 1, False: 2})
    )
    df_apc['CHANGE_TYPE_PRIORITY'] = (
        (df_apc['CHANGE_TYPE'] == 'UPDATEPARAMETERS').map({True: 1, False: 2})
    )

    area_rows = df_apc[df_apc['ATTRIBUTE_NAME'] == 'AREA'][['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID', 'ATTRIBUTE_VALUE']]
    area_rows = area_rows.rename(columns={'ATTRIBUTE_VALUE': 'JOB_AREA'}).drop_duplicates(
        subset=['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID'],
        keep='first',
    )
    df_apc = df_apc.merge(area_rows, on=['WAFER_ID', 'APC_OPERATION', 'APC_DATA_ID'], how='left')
    df_apc['AREA_PRIORITY'] = (df_apc['JOB_AREA'] == target_area).map({True: 1, False: 2})

    null_vals = {'', '[NULL]', 'nan', 'None'}
    df_apc['NULL_PRIORITY'] = (
        df_apc['ATTRIBUTE_VALUE'].isna() | df_apc['ATTRIBUTE_VALUE'].astype(str).str.strip().isin(null_vals)
    ).astype(int)

    df_apc_dedup = (
        df_apc.sort_values([
            'NULL_PRIORITY',
            'SUBENTITY_MATCH_PRIORITY',
            'APC_SYSTEM_PRIORITY',
            'CHANGE_TYPE_PRIORITY',
            'AREA_PRIORITY',
            'TXN_DATE',
        ])
        .drop_duplicates(subset=['WAFER_ID', 'APC_OPERATION', 'ATTRIBUTE_NAME'], keep='first')
        .drop(
            [
                'SUBENTITY_MATCH_PRIORITY',
                'APC_SYSTEM_PRIORITY',
                'CHANGE_TYPE_PRIORITY',
                'AREA_PRIORITY',
                'NULL_PRIORITY',
                'JOB_SUBENTITY',
                'SRC_SUBENTITY',
                'JOB_AREA',
                'APC_DATA_ID',
            ],
            axis=1,
            errors='ignore',
        )
    )

    df_apc_wide, _ = base_module.safe_pivot_with_prefix_fixed(df_apc_dedup)
    df_apc_wide['WAFER_ID'] = df_apc_wide['WAFER_ID'].astype(str)
    df_apc_wide['APC_OPERATION'] = df_apc_wide['APC_OPERATION'].astype(str)

    out = df_filtered.merge(
        df_apc_wide,
        left_on=['WAFER_ID', 'WEC_OPERATION'],
        right_on=['WAFER_ID', 'APC_OPERATION'],
        how='left',
    )
    if 'APC_OPERATION' in out.columns:
        out = out.drop(columns=['APC_OPERATION'])

    out = base_module.apply_ube_subentity_extraction(out)

    apc_cols = [c for c in out.columns if c.startswith('APC_')]
    out['APC_MATCHED'] = out[apc_cols].notna().any(axis=1) if apc_cols else False
    return out


def build_output_path(output_dir: Path, input_path: Path, target_area: str) -> Path:
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    area_slug = target_area.replace(' ', '_')
    filename = f'{input_path.stem}_APC_EXPLORATORY_{area_slug}_AME417PM_{ts}.csv'
    return output_dir / filename


def summarize(df_out: pd.DataFrame, target_area: str):
    logger.info('=' * 72)
    logger.info('EXPLORATORY APC JOIN SUMMARY')
    logger.info('=' * 72)
    logger.info('Output rows: %s', len(df_out))
    logger.info('Distinct wafers: %s', df_out['WAFER_ID'].nunique() if 'WAFER_ID' in df_out.columns else 0)

    if 'SUBENTITY' in df_out.columns:
        logger.info('SUBENTITY breakdown:')
        for subentity, count in df_out['SUBENTITY'].value_counts().items():
            logger.info('  %s: %s', subentity, count)

    if 'APC_MATCHED' in df_out.columns:
        matched = int(df_out['APC_MATCHED'].sum())
        coverage = (matched / len(df_out) * 100.0) if len(df_out) else 0.0
        logger.info('Rows with APC data: %s (%.1f%%)', matched, coverage)

    if 'APC_AREA' in df_out.columns:
        logger.info('APC_AREA distribution:')
        area_counts = df_out['APC_AREA'].fillna('[NULL]').value_counts(dropna=False)
        for area, count in area_counts.items():
            logger.info('  %s: %s', area, count)
        non_null = df_out['APC_AREA'].dropna()
        wrong_area = non_null[non_null != target_area]
        if len(wrong_area) > 0:
            logger.warning('Found %s rows with APC_AREA != %s', len(wrong_area), target_area)

    missing_pairs = 0
    if {'WAFER_ID', 'WEC_OPERATION', 'APC_MATCHED'}.issubset(df_out.columns):
        missing_pairs = df_out.loc[~df_out['APC_MATCHED'], ['WAFER_ID', 'WEC_OPERATION']].drop_duplicates().shape[0]
        logger.info('Unmatched wafer-operation pairs: %s', missing_pairs)

    logger.info('=' * 72)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Exploratory APC join for 1278 HCCD D1V AME417 PM chambers with strict AREA filter.'
    )
    parser.add_argument('--input-csv', default=DEFAULT_INPUT, help='Path to integrated source CSV.')
    parser.add_argument('--output-dir', default=DEFAULT_OUTPUT_DIR, help='Directory for exploratory output CSV.')
    parser.add_argument('--database', default=DEFAULT_DATABASE, help='PyUber database connection name.')
    parser.add_argument('--target-area', default=DEFAULT_TARGET_AREA, help='Strict APC AREA filter value.')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE, help='Wafer chunk size for APC queries.')
    parser.add_argument('--sample-days', type=int, default=DEFAULT_SAMPLE_DAYS, help='Limit to rows within last N days by DATA_COLLECTION_TIME; set 0 to disable.')
    return parser.parse_args()


def main():
    args = parse_args()
    start = datetime.now()

    input_path = Path(args.input_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parent

    logger.info('Loading source CSV: %s', input_path)
    df_input = pd.read_csv(input_path)
    logger.info('Source rows: %s', len(df_input))

    df_filtered = filter_source_rows(df_input, sample_days=args.sample_days)
    if df_filtered.empty:
        logger.warning('No rows remain after SUBENTITY/time filters; writing empty exploratory output.')

    base_module = load_base_apc_module(repo_root)

    df_apc_raw, query_stats = query_apc_strict_area(
        base_module=base_module,
        df_filtered=df_filtered,
        database_connection=args.database,
        target_area=args.target_area,
        chunk_size=args.chunk_size,
    )

    logger.info(
        'Query stats: operations=%s chunks=%s AEPCMC_rows=%s AEPC2_rows=%s remaining_unmatched_wafers=%s',
        query_stats['operations'],
        query_stats['chunks'],
        query_stats['aepcmc_rows'],
        query_stats['aepc2_rows'],
        query_stats['remaining_unmatched_wafers'],
    )

    df_out = dedup_pivot_merge(
        base_module=base_module,
        df_filtered=df_filtered,
        df_apc_raw=df_apc_raw,
        target_area=args.target_area,
    )

    out_path = build_output_path(output_dir=output_dir, input_path=input_path, target_area=args.target_area)
    logger.info('Writing exploratory output: %s', out_path)
    df_out.to_csv(out_path, index=False)

    summarize(df_out=df_out, target_area=args.target_area)
    logger.info('Total runtime: %s', datetime.now() - start)
    logger.info('Exploratory APC output ready: %s', out_path)


if __name__ == '__main__':
    main()
