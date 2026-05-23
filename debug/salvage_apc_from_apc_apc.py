"""
salvage_apc_from_apc_apc.py  —  one-off salvage script

Root cause of _APC_APC.csv:
    The prior backfill command passed *_APC.csv files* (already enriched) as
    input_csv_path to run_apc_join().  That function derives the output path as
    input.stem + '_APC', so the enriched file became the source and the output
    landed in _APC_APC.csv.

    The merge of the enriched left side (had APC_* cols) + fresh right side
    (also had APC_* cols) produced _x (old) / _y (fresh) collision suffixes for
    every APC_* column.  Post-processing (extraction, reorder) uses exact column
    names, so both steps were silently skipped for the suffixed columns.

What this script does:
    1. Loads each *_APC_APC.csv
    2. Drops all *_x columns  (stale values from the incorrectly-passed APC file)
    3. Renames *_y → clean names  (these ARE the authoritative fresh APC data)
    4. Creates APC_B_TOOL_RAW, APC_M_ETCHRATE_RAW, APC_SETTING_USED_RAW backups
    5. Applies _extract_first_value scalar extraction to all vector columns
    6. Calls _reorder_columns_apc for correct column ordering
    7. Writes to the correct *_APC.csv (overwrites)

After verifying the output files, delete the *_APC_APC.csv artifacts manually.

Usage:
    & 'C:/users/tbatson/My Programs/SQLPathFinder3/Python3/python.exe'
      '\\orshfs.intel.com\...\sDTT_rev01\debug\salvage_apc_from_apc_apc.py'
"""
import importlib.util
import sys
from pathlib import Path

import pandas as pd

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(r'\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01')
IO   = ROOT / 'integrated_output'

JOBS = [
    ('1278sDTT_HCCD_D1V_APC_APC.csv', '1278sDTT_HCCD_D1V_APC.csv'),
    ('1278sDTT_HCCD_F32_APC_APC.csv', '1278sDTT_HCCD_F32_APC.csv'),
]

# Vector columns that may contain comma/semicolon-delimited payloads.
# Mirrors the _vector_cols list in main_cascading_area_priority_final().
_VECTOR_COLS = [
    'APC_B_TOOL', 'APC_B_TOOL_RS', 'APC_B_PART', 'APC_B_PART_PRIOR',
    'APC_B_TOOL_PRIOR', 'APC_CALCULATED_SETTING', 'APC_REFERENCE_SETTING',
    'APC_LAMBDA_DRIFT', 'APC_LAMBDA_TOOL', 'APC_LAMBDA_POSTPM',
    'APC_LAMBDA_POSTPM_TOOL', 'APC_LAMBDA_PART',
    'APC_M_ETCHRATE', 'APC_SETTING_USED',
]

# Columns to preserve as _RAW before extraction (per APC KB spec).
_RAW_PRESERVE = ['APC_B_TOOL', 'APC_M_ETCHRATE', 'APC_SETTING_USED']

# ── Load helpers from APC JOIN module (no DB connection opened) ────────────────
_apc_path = ROOT / '1278sDTT_D1V_HCCD_APC_JOIN.py'
_spec = importlib.util.spec_from_file_location('_apc_mod', str(_apc_path))
_apc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_apc_mod)

_extract_first_value = _apc_mod._extract_first_value
_reorder_columns_apc = _apc_mod._reorder_columns_apc

# ── Process each file ──────────────────────────────────────────────────────────
for src_name, out_name in JOBS:
    src_path = IO / src_name
    out_path = IO / out_name

    if not src_path.exists():
        print(f'SKIP {src_name} — file not found')
        continue

    print(f'\n{"=" * 70}')
    print(f'  Source : {src_name}')
    print(f'  Output : {out_name}')
    print(f'{"=" * 70}')

    df = pd.read_csv(str(src_path), low_memory=False)
    print(f'  Loaded: {len(df):,} rows × {len(df.columns)} columns')

    # ── Classify columns ───────────────────────────────────────────────────────
    all_cols = df.columns.tolist()
    x_cols   = [c for c in all_cols if c.endswith('_x')]
    y_cols   = [c for c in all_cols if c.endswith('_y')]
    print(f'  _x cols (dropped) : {len(x_cols)}  {x_cols[:5]}{"..." if len(x_cols) > 5 else ""}')
    print(f'  _y cols (renamed) : {len(y_cols)}  {y_cols[:5]}{"..." if len(y_cols) > 5 else ""}')

    # ── Drop _x, rename _y ────────────────────────────────────────────────────
    df_clean = df.drop(columns=x_cols).copy()
    rename_map = {c: c[:-2] for c in y_cols}   # strip trailing '_y'
    df_clean = df_clean.rename(columns=rename_map)
    print(f'  After cleanup: {len(df_clean):,} rows × {len(df_clean.columns)} columns')

    # Sanity check: no remaining _x/_y column names
    leftover = [c for c in df_clean.columns if c.endswith('_x') or c.endswith('_y')]
    if leftover:
        print(f'  WARNING: unexpected suffixed columns still present: {leftover}')

    # ── Create _RAW backups before in-place extraction ─────────────────────────
    created_raw = []
    for rc in _RAW_PRESERVE:
        if rc in df_clean.columns:
            df_clean[f'{rc}_RAW'] = df_clean[rc].copy()
            created_raw.append(f'{rc}_RAW')
    print(f'  _RAW columns created: {created_raw}')

    # ── Apply scalar extraction to vector/matrix columns ──────────────────────
    extracted = []
    for vc in _VECTOR_COLS:
        if vc in df_clean.columns:
            df_clean[vc] = df_clean[vc].apply(_extract_first_value)
            extracted.append(vc)
    print(f'  Extraction applied to {len(extracted)} cols: {extracted}')

    # ── Column reorder ─────────────────────────────────────────────────────────
    df_clean = _reorder_columns_apc(df_clean)

    # ── Validation ─────────────────────────────────────────────────────────────
    if 'APC_B_TOOL' in df_clean.columns:
        total      = len(df_clean)
        non_null   = df_clean['APC_B_TOOL'].notna().sum()
        comma_cnt  = df_clean['APC_B_TOOL'].astype(str).str.contains(',', na=False).sum()
        print(f'  APC_B_TOOL: {non_null:,}/{total:,} non-null ({non_null/total*100:.1f}%), '
              f'{comma_cnt} rows still contain commas')

    if 'APC_B_TOOL_RAW' in df_clean.columns:
        raw_sample = (df_clean.loc[df_clean['APC_B_TOOL_RAW'].notna(), 'APC_B_TOOL_RAW']
                      .head(3).tolist())
        print(f'  APC_B_TOOL_RAW sample (first 3 non-null): {raw_sample}')

    print(f'  Final shape: {df_clean.shape}')

    # ── Write ──────────────────────────────────────────────────────────────────
    df_clean.to_csv(str(out_path), index=False)
    print(f'  Written to: {out_path}')

print(f'\n{"=" * 70}')
print('Salvage complete.')
print()
print('Next steps:')
print('  1. Inspect the output _APC.csv files to confirm APC_B_TOOL has no commas,')
print('     APC_B_TOOL_RAW holds the original vector strings, and row counts look right.')
print('  2. Once satisfied, delete the _APC_APC.csv artifacts:')
for _, out_name in JOBS:
    src_name = out_name.replace('_APC.csv', '_APC_APC.csv')
    print(f'       del "{IO / src_name}"')
print(f'{"=" * 70}')
