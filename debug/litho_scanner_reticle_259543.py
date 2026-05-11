"""
Diagnostic query: fetch SCANNER and RETICLE for litho operation 259543
for wafers that ran etch operation 266124 (E_M6_HM_ETCH) but are missing
scanner/reticle data in the sDTT output (SNIP-route gap).

Operation 259543 is a P1280 M6 litho step not currently in the
1280_oper_alias_lookup WEC alias for E_M6_HM_ETCH.

Wafers to check: those in the 1280sDTT HCCD D1V output that have
etch_oper=266124 and null SCANNER/RETICLE.
"""

import PyUber
import pandas as pd
import os

# ---------------------------------------------------------------------------
# Target wafers (etch oper 266124, missing scanner/reticle in output)
# ---------------------------------------------------------------------------
WAFER_IDS = [
    'MC7YK303WAB6',
    'MC7YK293WAG2',
    'MC7YK261WAG3',
    'MC7YK294WAD1',
    'MC7YK272WAA3',
    'MC7YK255WAE1',
    '5DWCW042MVF6',
    '5DWEM495MVC7',
    '5DVZG284MVD1',
    'MC9QL400WAF1',
    '5DWFY744MVD1',
    '5DWFY721MVD4',
    '5DWFY717MVC3',
    '5DWGF148MVG4',
    '5DWEM517MVE0',
    'GZ1WA441JKF0',
]

LITHO_OPER = 259543   # M6 litho op not in current alias
DB         = 'D1D_PROD_XEUS_LOCAL'

# ---------------------------------------------------------------------------
# Build query
# ---------------------------------------------------------------------------
wafer_str = ', '.join(f"'{w}'" for w in WAFER_IDS)

sql = f"""
SELECT DISTINCT
    c.WAFER          "WAFER_ID"
   ,h.OPERATION
   ,c.ENTITY         "SCANNER"
   ,c.SUBENTITY
   ,w.RETICLE
   ,CAST(w.WAFER_ENTITY_END_TIME AS DATE)   "LITHO_ENTITY_END_TIME"
FROM F_LOT_RUN_MAP h
INNER JOIN F_WAFERENTITYHIST w
    ON  w.RUNKEY           = h.RUNKEY
    AND w.EXPECTED_LOT     = h.EXPECTED_LOT
    AND w.WAFER            IS NOT NULL
    AND w.IS_CONDITIONING_WAFER IS NULL
INNER JOIN F_WAFERCHAMBERHIST c
    ON  c.RUNKEY  = w.RUNKEY
    AND c.WAFER   = w.WAFER
    AND c.ENTITY  = w.ENTITY
WHERE h.OPERATION IN ({LITHO_OPER})
  AND c.WAFER    IN ({wafer_str})
ORDER BY c.WAFER
"""

# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------
print(f"Querying {DB} for oper {LITHO_OPER} on {len(WAFER_IDS)} wafers...")
print()

db  = PyUber.connect(DB)
df  = pd.read_sql(sql, db)

print(f"Rows returned: {len(df)}")
print()
print(df.to_string(index=False))

# ---------------------------------------------------------------------------
# Save alongside other debug outputs
# ---------------------------------------------------------------------------
out_dir  = os.path.dirname(os.path.abspath(__file__))
out_path = os.path.join(out_dir, 'litho_259543_scanner_reticle.csv')
df.to_csv(out_path, index=False)
print(f"\nSaved → {out_path}")

# ---------------------------------------------------------------------------
# Quick coverage check: which of the 16 input wafers got a hit?
# ---------------------------------------------------------------------------
found    = set(df['WAFER_ID'].unique())
missing  = [w for w in WAFER_IDS if w not in found]
print(f"\nCoverage: {len(found)}/{len(WAFER_IDS)} wafers returned results")
if missing:
    print("No rows for:")
    for w in missing:
        print(f"  {w}")
