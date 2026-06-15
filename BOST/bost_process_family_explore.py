"""
BOST/bost_process_family_explore.py

Wafer-level YieldProcessDefinitions query for the full HM flow, filtered to
wafers/lots present in the HCCD 60-day APC CSV within a recent lookback window.

Full flow aliases (M5–M14):
  L_8M{n}_SIARC_DEP   SiARC deposition  (before litho)
  L_8M{n}_CHM_DEP     CHM  deposition   (before litho)
  L_8M{n}_SED         Litho SED
  E_8M{n}_HM_ETCH     Dry HM etch       (AMEct)
  W_8M{n}_HM_CLN      Wet HM clean      (LEOcb, after etch)

Three-phase execution:
  Phase A — F_OPERATION_ALIAS lookup (HCCD/DCCD CD aliases → numeric op numbers)
  Phase B — TRIGGER_OPERATION filter construction (alias strings + numeric ops)
  Phase C — Wafer-level BOST query scoped to source wafer list
"""

import warnings
warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

import re
import PyUber
import pandas as pd
from datetime import datetime, timedelta

# ── Configuration ──────────────────────────────────────────────────────────────
DSN         = "D1D_PROD_XEUS_GAJT"
PROCESS     = "1278"
FAB         = "D1D"
DATA_SOURCE = "D1_P1278"

LAYERS = [6, 7, 8, 9, 10]   # adhoc: M6-M10 only

# SED WEC aliases only
WEC_ALIASES = [f"L_8M{n}_SED" for n in LAYERS]

# DCCD CD aliases only (SED → DCCD)
CD_ALIASES = [f"L_8M{n}_DCCD" for n in LAYERS]

# Source wafer list
APC_CSV = (
    r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME"
    r"\tbatson\sDTT\sDTT_rev01\integrated_output\1278sDTT_HCCD_D1V_60day_APC.csv"
)
LOOKBACK_DAYS = 60  # adhoc: full 60-day window

# ── Phase A SQL ────────────────────────────────────────────────────────────────
_CD_ALIASES_SQL = ", ".join(f"'{a}'" for a in CD_ALIASES)

OPERALIAS_SQL = f"""
SELECT a.OPERATION
      ,a.OPER_GROUP_NAME        AS ALIAS
      ,a.OPER_INTEGRATION_LAYER AS LAYER
FROM   F_OPERATION_ALIAS a
WHERE  a.DATA_SOURCE IN ('{DATA_SOURCE}')
  AND  UPPER(a.OPER_GROUP_NAME) IN ({_CD_ALIASES_SQL})
ORDER BY a.OPER_GROUP_NAME, a.OPERATION
"""

# ── Phase C SQL template ───────────────────────────────────────────────────────
BOST_SQL_TEMPLATE = """
SELECT
   w.LOT
  ,w.WAFER
  ,f.PROCESS_FAMILY
  ,f.TRIGGER_OPERATION
  ,d.DEFINITION_NAME
  ,d.USAGE
  ,d.VERSION
  ,d.IS_LATEST
  ,d.IS_ACTIVE
  ,d.LAST_MODIFY_USER
  ,v.PROC_STRING_VALUE
FROM B_META_WAFER_FAB w
INNER JOIN B_WAFER_PROCESS_DEFN v
  ON  v.WAFER_KEY = w.WAFER_KEY
INNER JOIN B_CFG_PROCESS_DEFN_FAMILY f
  ON  f.PROCESS_FAMILY_ID = v.PROCESS_FAMILY_ID
INNER JOIN B_CFG_PROCESS_DEFN d
  ON  d.DEFINITION_ID = f.DEFINITION_ID
  AND d.PROCESS = '{process}'
  AND d.FAB IN ('{fab}')
  AND d.IS_LATEST = 'Y'
  AND d.IS_ACTIVE = 'Y'
WHERE {lots_filter}
  AND {wafers_filter}
  AND (
  {trigger_filter}
  )
ORDER BY w.LOT, w.WAFER, f.PROCESS_FAMILY
"""


def _chunked_in_clause(col, values, chunk_size=999):
    """Return a SQL OR-chain of IN clauses to stay under Oracle's 1000-item limit."""
    chunks = [values[i:i + chunk_size] for i in range(0, len(values), chunk_size)]
    parts  = [f"{col} IN ({', '.join(repr(v) for v in chunk)})" for chunk in chunks]
    return "(" + "\n  OR ".join(parts) + ")"


def _build_trigger_filter(wec_aliases, numeric_ops):
    """OR-chain of INSTR conditions for alias strings and numeric op numbers."""
    terms = [f"INSTR(f.TRIGGER_OPERATION, '{a}') > 0" for a in wec_aliases]
    terms += [f"INSTR(f.TRIGGER_OPERATION, '{op}') > 0" for op in numeric_ops]
    return "\n  OR ".join(terms)


def _load_source_wafers(csv_path, lookback_days):
    """Return (lots, wafers) lists from the APC CSV filtered to recent window."""
    cutoff = datetime.now() - timedelta(days=lookback_days)
    df = pd.read_csv(
        csv_path,
        usecols=["DATA_COLLECTION_TIME", "SPC_LOT", "WAFER_ID"],
        parse_dates=["DATA_COLLECTION_TIME"],
    )
    df = df[df["DATA_COLLECTION_TIME"] >= cutoff]
    lots   = sorted(df["SPC_LOT"].dropna().unique().tolist())
    wafers = sorted(df["WAFER_ID"].dropna().unique().tolist())
    return lots, wafers, df["DATA_COLLECTION_TIME"].min(), df["DATA_COLLECTION_TIME"].max()


def _in_clause(values):
    """Format a list as a SQL IN-list string."""
    return ", ".join(f"'{v}'" for v in values)


def main():
    # ── Load source wafers from APC CSV ───────────────────────────────────────
    print(f"Loading source wafers from APC CSV (last {LOOKBACK_DAYS} days) ...")
    lots, wafers, t_min, t_max = _load_source_wafers(APC_CSV, LOOKBACK_DAYS)
    print(f"  Window   : {t_min}  →  {t_max}")
    print(f"  Lots     : {len(lots)}")
    print(f"  Wafers   : {len(wafers)}")
    if not lots:
        print("No wafers found in window — exiting.")
        return

    print(f"\nDSN        : {DSN}")
    print(f"WEC aliases: {len(WEC_ALIASES)}  [{WEC_ALIASES[0]} .. {WEC_ALIASES[-1]}]")
    print("-" * 60)

    conn = PyUber.connect(DSN)
    try:
        # ── Phase A ───────────────────────────────────────────────────────────
        print("Phase A: F_OPERATION_ALIAS lookup ...")
        df_alias = pd.read_sql(OPERALIAS_SQL, conn)
        numeric_ops = sorted(df_alias["OPERATION"].dropna().astype(str).unique().tolist())
        print(f"  {len(df_alias)} alias rows  |  {len(numeric_ops)} unique operation numbers")

        # ── Phase B ───────────────────────────────────────────────────────────
        trigger_filter = _build_trigger_filter(WEC_ALIASES, numeric_ops)

        # ── Phase C ───────────────────────────────────────────────────────────
        bost_sql = BOST_SQL_TEMPLATE.format(
            process=PROCESS,
            fab=FAB,
            lots_filter=_chunked_in_clause("w.LOT", lots),
            wafers_filter=_chunked_in_clause("w.WAFER", wafers),
            trigger_filter=trigger_filter,
        )
        print("Phase C: wafer-level BOST query ...")
        df = pd.read_sql(bost_sql, conn)
    finally:
        conn.close()

    # ── Add LAYER column — matches sDTT CSV LAYER convention ─────────────────
    # MT5–MT9 for layers 5-9, M10–M14 for layers 10-14, BM0 for BM0 layer.
    # Source: confirmed from integrated_output/1278sDTT_HCCD_D1V_60day_APC.csv.
    def _trig_to_layer(trig):
        s = str(trig)
        if "BM0" in s:
            return "BM0"
        m = re.search(r'M(\d+)', s)
        if not m:
            return None
        n = int(m.group(1))
        return f"MT{n}" if n <= 9 else f"M{n}"
    df.insert(2, "LAYER", df["TRIGGER_OPERATION"].map(_trig_to_layer))

    print(f"\nResult shape : {df.shape[0]} rows x {df.shape[1]} cols")

    # ── Unique (PROCESS_FAMILY, TRIGGER_OPERATION) combos ─────────────────────
    combos = (
        df[["PROCESS_FAMILY", "TRIGGER_OPERATION"]]
        .drop_duplicates()
        .sort_values("PROCESS_FAMILY")
    )
    pd.set_option("display.max_colwidth", 60)
    print(f"\n--- Unique (PROCESS_FAMILY, TRIGGER_OPERATION) combos ({len(combos)}) ---")
    print(combos.to_string(index=False))

    # ── Within-lot wafer-level variance check ─────────────────────────────────
    variance = (
        df.groupby(["LOT", "PROCESS_FAMILY"])["PROC_STRING_VALUE"]
          .nunique()
          .rename("distinct_proc_values")
          .reset_index()
    )
    splits = variance[variance["distinct_proc_values"] > 1]
    print(f"\n--- Within-lot PROC_STRING_VALUE splits "
          f"({len(splits)} families have wafer-level variation) ---")
    if not splits.empty:
        print(splits.to_string(index=False))

    # ── First 30 rows ──────────────────────────────────────────────────────────
    print("\n--- First 30 rows ---")
    print(df.head(30).to_string(index=False))

    # ── Save ───────────────────────────────────────────────────────────────────
    date_tag = datetime.now().strftime("%Y%m%d")
    out_path = (
        r"\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME"
        rf"\tbatson\sDTT\sDTT_rev01\BOST\bost_fullflow_output_{date_tag}.csv"
    )
    df.to_csv(out_path, index=False)
    print(f"\nSaved {len(df):,} rows -> {out_path}")


if __name__ == "__main__":
    main()


