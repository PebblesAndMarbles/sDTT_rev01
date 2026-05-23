"""
debug/run_mt5h_replace.py  —  one-off MT5H strict APC replacement script

Re-queries MT5H rows (operation 263067) for both D1V and F32 using strict
require_area_btool mode, then replaces the MT5H slice in each production
_APC.csv.  Intended to run after the cascade bug-fix applied to
try_apc_system_with_area_cascade() in 1278sDTT_D1V_HCCD_APC_JOIN.py.

The cascade bug that this corrects:
    Partial FLOW_TEMP rows (AREA present, B_TOOL absent) for unmatched MT5H
    wafers were appended to results[] BEFORE the match check, so those wafers
    also picked up rows from the next area tier (AMECT_ICCR2).  After
    dedup-by-TXN_DATE, the winner for AREA could be FLOW_TEMP while B_TOOL
    came from AMECT_ICCR2 — cross-tier contamination.  The fix discards
    partial rows for unmatched wafers at each tier before cascading down.

Usage:
    & 'C:/users/tbatson/My Programs/SQLPathFinder3/Python3/python.exe'
      '\\orshfs.intel.com\...\sDTT_rev01\debug\run_mt5h_replace.py'
"""
import importlib.util
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(r'\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01')
IO   = ROOT / 'integrated_output'

# ── MT5H scope constants ───────────────────────────────────────────────────────
MT5H_OPERATION = '263067'
MT5H_TEST_NAME = '8CD.FCCD.MT5H'
MT5H_LAYER     = 'MT5'

JOBS = [
    {
        'site':     'D1V',
        'base_csv': IO / '1278sDTT_HCCD_D1V.csv',
        'prod_apc': IO / '1278sDTT_HCCD_D1V_APC.csv',
    },
    {
        'site':     'F32',
        'base_csv': IO / '1278sDTT_HCCD_F32.csv',
        'prod_apc': IO / '1278sDTT_HCCD_F32_APC.csv',
    },
]

# ── Load APC JOIN module (no DB connection opened at import time) ──────────────
_apc_path = ROOT / '1278sDTT_D1V_HCCD_APC_JOIN.py'
_spec = importlib.util.spec_from_file_location('_apc_mod', str(_apc_path))
_apc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_apc_mod)
run_apc_join = _apc_mod.run_apc_join


# ── Helpers ────────────────────────────────────────────────────────────────────
def _mt5h_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask for MT5H rows — OR logic across available columns."""
    mask = pd.Series(False, index=df.index)
    if 'WEC_OPERATION' in df.columns:
        mask |= df['WEC_OPERATION'].astype(str) == MT5H_OPERATION
    if 'TEST_NAME' in df.columns:
        mask |= df['TEST_NAME'].astype(str).str.contains(MT5H_TEST_NAME, na=False, regex=False)
    if 'LAYER' in df.columns:
        mask |= df['LAYER'].astype(str).str.contains(MT5H_LAYER, na=False, regex=False)
    return mask


_NULL_STRS = {'', '[NULL]', 'nan', 'None', 'NULL'}

def _null_rate(series: pd.Series) -> float:
    if len(series) == 0:
        return float('nan')
    n_null = (series.isna() | series.astype(str).str.strip().isin(_NULL_STRS)).sum()
    return n_null / len(series) * 100


# ── Main loop ──────────────────────────────────────────────────────────────────
stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
temp_files_created = []

for job in JOBS:
    site     = job['site']
    base_csv = job['base_csv']
    prod_apc = job['prod_apc']

    print(f'\n{"=" * 70}')
    print(f'  Site     : {site}')
    print(f'  Base CSV : {base_csv.name}')
    print(f'  Prod APC : {prod_apc.name}')
    print(f'{"=" * 70}')

    if not base_csv.exists():
        print(f'  SKIP — base CSV not found: {base_csv}')
        continue
    if not prod_apc.exists():
        print(f'  SKIP — production APC CSV not found: {prod_apc}')
        continue

    # ── Step 1: Extract MT5H rows from base HCCD CSV ──────────────────────
    df_base    = pd.read_csv(str(base_csv), low_memory=False)
    mt5h_mask  = _mt5h_mask(df_base)
    df_mt5h_in = df_base[mt5h_mask].copy()
    print(f'  MT5H rows in base CSV : {len(df_mt5h_in):,} of {len(df_base):,}')

    if df_mt5h_in.empty:
        print(f'  SKIP — zero MT5H rows found for {site}')
        continue

    # ── Step 2: Write temp MT5H input CSV ─────────────────────────────────
    temp_input = IO / f'1278sDTT_HCCD_{site}_MT5H_STRICT_INPUT_{stamp}.csv'
    df_mt5h_in.to_csv(str(temp_input), index=False)
    temp_files_created.append(temp_input)
    print(f'  Temp input written    : {temp_input.name}')

    # ── Step 3: Run strict APC join for MT5H rows only ────────────────────
    print(f'  Running strict APC join (require_area_btool op={MT5H_OPERATION}) ...')
    sys.stdout.flush()
    mt5h_apc_out = run_apc_join(
        str(temp_input),
        require_area_btool_for_match_ops=[MT5H_OPERATION],
        site=site,
    )

    if not mt5h_apc_out:
        print(f'  ERROR — run_apc_join returned empty path; aborting {site}')
        continue

    mt5h_apc_path = Path(mt5h_apc_out)
    temp_files_created.append(mt5h_apc_path)

    if not mt5h_apc_path.exists():
        print(f'  ERROR — APC output not found: {mt5h_apc_path}; aborting {site}')
        continue

    # ── Step 4: Read and scope-guard the APC output ────────────────────────
    df_mt5h_new = pd.read_csv(str(mt5h_apc_path), low_memory=False)
    new_scope   = _mt5h_mask(df_mt5h_new)
    df_mt5h_new = df_mt5h_new[new_scope].copy()
    print(f'  APC output MT5H rows (after scope guard) : {len(df_mt5h_new):,}')

    if df_mt5h_new.empty:
        print(f'  ERROR — APC output has zero MT5H rows after scope guard; aborting {site}')
        continue

    # ── Step 5: Load production APC and record before-stats ───────────────
    df_prod       = pd.read_csv(str(prod_apc), low_memory=False)
    prod_scope    = _mt5h_mask(df_prod)
    df_prod_other = df_prod[~prod_scope].copy()
    df_prod_old   = df_prod[prod_scope].copy()
    print(f'  Production APC total  : {len(df_prod):,} rows, '
          f'{len(df_prod_old):,} MT5H rows being replaced')

    if 'APC_B_TOOL' in df_prod_old.columns and len(df_prod_old) > 0:
        print(f'  APC_B_TOOL null rate BEFORE : {_null_rate(df_prod_old["APC_B_TOOL"]):.1f}%')
    if 'APC_AREA' in df_prod_old.columns and len(df_prod_old) > 0:
        area_before = df_prod_old['APC_AREA'].value_counts(dropna=False)
        print(f'  APC_AREA (before) : {area_before.to_dict()}')

    # ── Step 6: Backup production APC CSV ─────────────────────────────────
    backup = prod_apc.with_name(
        f'{prod_apc.stem}.bak_mt5h_replace_{stamp}{prod_apc.suffix}'
    )
    shutil.copy2(str(prod_apc), str(backup))
    print(f'  Backup created        : {backup.name}')

    # ── Step 7: Align new slice to production schema ───────────────────────
    for col in df_prod.columns:
        if col not in df_mt5h_new.columns:
            df_mt5h_new[col] = pd.NA
    df_mt5h_new = df_mt5h_new[df_prod.columns]

    # ── Step 8: Splice, sort, write ───────────────────────────────────────
    df_final = pd.concat([df_prod_other, df_mt5h_new], ignore_index=True)
    if 'DATA_COLLECTION_TIME' in df_final.columns:
        _ts = pd.to_datetime(
            df_final['DATA_COLLECTION_TIME'], format='mixed', dayfirst=False, errors='coerce'
        )
        df_final = (df_final
                    .assign(_sort=_ts)
                    .sort_values('_sort', ascending=False)
                    .drop(columns=['_sort'])
                    .reset_index(drop=True))

    expected = len(df_prod_other) + len(df_mt5h_new)
    if len(df_final) != expected:
        print(f'  WARNING — final row count {len(df_final):,} != expected {expected:,}')

    df_final.to_csv(str(prod_apc), index=False)

    # After-stats
    final_scope   = _mt5h_mask(df_final)
    df_final_mt5h = df_final[final_scope]
    if 'APC_B_TOOL' in df_final_mt5h.columns and len(df_final_mt5h) > 0:
        print(f'  APC_B_TOOL null rate AFTER  : {_null_rate(df_final_mt5h["APC_B_TOOL"]):.1f}%')
    if 'APC_AREA' in df_final_mt5h.columns and len(df_final_mt5h) > 0:
        area_after = df_final_mt5h['APC_AREA'].value_counts(dropna=False)
        print(f'  APC_AREA (after)  : {area_after.to_dict()}')

    print(f'  Written: {prod_apc.name}  ({len(df_final):,} total rows)')


# ── Summary ────────────────────────────────────────────────────────────────────
print(f'\n{"=" * 70}')
print('MT5H replacement complete.')
if temp_files_created:
    print()
    print('Temp files to delete after verification:')
    for f in temp_files_created:
        print(f'  del "{f}"')
print(f'{"=" * 70}')
