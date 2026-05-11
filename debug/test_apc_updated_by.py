"""
test_apc_updated_by.py
----------------------
Diagnostic query to check whether 'UPDATED_BY' and 'UPDATED_TIME' values are
accessible for recent HCCD/D1V APC records.

Two probes are run:

  Probe 1 – ATTRIBUTE_NAME scan
      Looks for UPDATED_BY / UPDATED_TIME as rows in P_APC_TXN_DATA
      (same pattern as all other APC attributes like B_TOOL, FB_SUC, etc.)

  Probe 2 – Column scan on P_APC_RUNJOB_HIST
      Checks whether UPDATED_BY / UPDATED_TIME exist as actual SQL columns
      on the job-header table (SELECT ... FROM P_APC_RUNJOB_HIST).

Test wafers / operations come from the 5 most-recent rows of the existing
1278sDTT_HCCD_D1V_APC.csv so that the query hits known-good APC job records.
"""

import pandas as pd
import PyUber
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
for _noisy in ('sqlalchemy.engine', 'sqlalchemy', 'PyUber', 'pyuber'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Configuration ─────────────────────────────────────────────────────────────
APC_CSV = (
    r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson"
    r"\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_D1V_APC.csv"
)
DATABASE = 'D1D_PROD_XEUS_LOCAL'
N_RECENT_ROWS = 10   # number of most-recent rows to draw test wafers from
# ─────────────────────────────────────────────────────────────────────────────


def load_test_wafers(csv_path: str, n: int):
    """Return (wafer_str, operation_str) SQL-ready snippets from the n most-recent rows."""
    df = pd.read_csv(csv_path, nrows=n)   # file is sorted DESC by DATA_COLLECTION_TIME
    wafers = df['WAFER_ID'].dropna().unique().tolist()
    ops    = df['WEC_OPERATION'].dropna().unique().tolist()
    wafer_str = "'" + "','".join(str(w) for w in wafers) + "'"
    op_str    = "'" + "','".join(str(o) for o in ops)    + "'"
    logger.info(f"Test wafers : {wafers}")
    logger.info(f"Test operations : {ops}")
    return wafer_str, op_str


# ── Probe 1: ATTRIBUTE_NAME rows ──────────────────────────────────────────────
PROBE1_QUERY = """
SELECT
    h.LOT7            AS LOT,
    w.WAFER            AS WAFER_ID,
    h.OPERATION        AS APC_OPERATION,
    j.APC_DATA_ID,
    j.APC_JOB_TXN_TIME AS TXN_DATE,
    j.APC_OBJECT_NAME,
    j.CHANGE_TYPE,
    d.ATTRIBUTE_NAME,
    d.ATTRIBUTE_VALUE
FROM F_LOT_FLOW h
INNER JOIN F_WAFERSLOTHIST w
    ON  w.EXPECTED_LOT = h.LOT
    AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
    AND w.HISTORY_DELETED_FLAG = 'N'
INNER JOIN P_APC_RUNJOB_HIST j
    ON j.LOTOPERKEY = h.LOTOPERKEY
INNER JOIN P_APC_TXN_DATA d
    ON d.APC_DATA_ID = j.APC_DATA_ID
WHERE w.WAFER         IN ({wafer_str})
  AND h.OPERATION     IN ({op_str})
  AND h.EXEC_FLAG     NOT IN ('X','R','N')
  AND j.APC_OBJECT_TYPE = 'LOT'
  AND d.ATTRIBUTE_NAME IN ('UPDATED_BY','UPDATED_TIME','B_TOOL','AREA')
ORDER BY j.APC_JOB_TXN_TIME DESC
"""

# ── Probe 2: column-level check on P_APC_RUNJOB_HIST ─────────────────────────
PROBE2_QUERY = """
SELECT
    h.LOT7            AS LOT,
    w.WAFER            AS WAFER_ID,
    h.OPERATION        AS APC_OPERATION,
    j.APC_DATA_ID,
    j.APC_JOB_TXN_TIME AS TXN_DATE,
    j.APC_OBJECT_NAME,
    j.CHANGE_TYPE,
    j.UPDATED_BY,
    j.UPDATED_TIME
FROM F_LOT_FLOW h
INNER JOIN F_WAFERSLOTHIST w
    ON  w.EXPECTED_LOT = h.LOT
    AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
    AND w.HISTORY_DELETED_FLAG = 'N'
INNER JOIN P_APC_RUNJOB_HIST j
    ON j.LOTOPERKEY = h.LOTOPERKEY
WHERE w.WAFER         IN ({wafer_str})
  AND h.OPERATION     IN ({op_str})
  AND h.EXEC_FLAG     NOT IN ('X','R','N')
  AND j.APC_OBJECT_TYPE = 'LOT'
ORDER BY j.APC_JOB_TXN_TIME DESC
"""


def run_probe(conn, label: str, query: str, wafer_str: str, op_str: str):
    q = query.format(wafer_str=wafer_str, op_str=op_str)
    print(f"\n{'='*70}")
    print(f"PROBE: {label}")
    print('='*70)
    print("SQL sent:\n")
    print(q)
    print()
    try:
        df = pd.read_sql(q, conn)
        if df.empty:
            print(">>> Result: EMPTY — no rows returned.")
        else:
            print(f">>> Result: {len(df)} rows  x  {len(df.columns)} columns")
            print(f">>> Columns: {list(df.columns)}")
            print()
            print(df.to_string(index=False, max_cols=20, max_rows=30))
        return df
    except Exception as e:
        print(f">>> QUERY FAILED: {e}")
        return None


def main():
    print("\n" + "="*70)
    print("APC UPDATED_BY / UPDATED_TIME — diagnostic test")
    print("="*70)

    wafer_str, op_str = load_test_wafers(APC_CSV, N_RECENT_ROWS)

    print(f"\nConnecting to {DATABASE} ...")
    conn = PyUber.connect(DATABASE)

    try:
        # Probe 1 — look for them as ATTRIBUTE_NAME rows in P_APC_TXN_DATA
        df1 = run_probe(conn, "P_APC_TXN_DATA  (ATTRIBUTE_NAME rows)",
                        PROBE1_QUERY, wafer_str, op_str)

        # Probe 2 — look for them as direct columns on P_APC_RUNJOB_HIST
        df2 = run_probe(conn, "P_APC_RUNJOB_HIST  (direct columns)",
                        PROBE2_QUERY, wafer_str, op_str)

    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)

    if df1 is not None and not df1.empty:
        found_attrs = df1['ATTRIBUTE_NAME'].unique().tolist()
        has_upd = [a for a in found_attrs if 'UPDATED' in a.upper()]
        if has_upd:
            print(f"[Probe 1] FOUND as ATTRIBUTE_NAME rows: {has_upd}")
            sample = df1[df1['ATTRIBUTE_NAME'].str.upper().str.contains('UPDATED')]
            print(sample[['WAFER_ID','APC_OPERATION','CHANGE_TYPE','APC_OBJECT_NAME',
                           'ATTRIBUTE_NAME','ATTRIBUTE_VALUE']].head(10).to_string(index=False))
        else:
            print("[Probe 1] UPDATED_BY / UPDATED_TIME NOT found as ATTRIBUTE_NAME rows.")
            print(f"          Attributes that were returned: {found_attrs}")
    else:
        print("[Probe 1] No rows returned or query failed.")

    print()

    if df2 is not None:
        if not df2.empty:
            print("[Probe 2] P_APC_RUNJOB_HIST columns query SUCCEEDED.")
            for col in ['UPDATED_BY', 'UPDATED_TIME']:
                if col in df2.columns:
                    non_null = df2[col].notna().sum()
                    print(f"  {col}: {non_null} non-null of {len(df2)} rows")
                    if non_null:
                        print(f"  Sample values: {df2[col].dropna().head(5).tolist()}")
                else:
                    print(f"  {col}: column NOT present in result set")
        else:
            print("[Probe 2] Query returned 0 rows (columns exist but no matching data).")
    else:
        print("[Probe 2] Column-level query FAILED — UPDATED_BY/UPDATED_TIME are "
              "probably NOT columns on P_APC_RUNJOB_HIST.")

    print("="*70)


if __name__ == "__main__":
    main()
