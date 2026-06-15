# BOST Defect Query — Layer Reference & Script Plan

Created: 2026-06-02  
Source workspace: `sDTT_rev01`  
Reference script: `BOST/bost_process_family_explore.py`

---

## 1. Full Layer Pattern Reference

Confirmed from `integrated_output/1278sDTT_HCCD_D1V_60day_APC.csv` (live production data).

| Numeric n | `LAYER` (sDTT CSV) | `ALLSTATS_MEASURED_LAYER` | `WEC_LAYER` (sDTT CSV) | Defect Layer token | BOST alias pattern (SED) |
|---|---|---|---|---|---|
| 5  | `MT5` | `MT5H` | `570_M05` | `8M5CL`  | `L_8M5_SED`  |
| 6  | `MT6` | `MT6H` | `580_M06` | `8M6CL`  | `L_8M6_SED`  |
| 7  | `MT7` | `MT7H` | `590_M07` | `8M7CL`  | `L_8M7_SED`  |
| 8  | `MT8` | `MT8H` | `600_M08` | `8M8CL`  | `L_8M8_SED`  |
| 9  | `MT9` | `MT9H` | `610_M09` | `8M9CL`  | `L_8M9_SED`  |
| 10 | `M10` | `M10H` | `620_M10` | `8M10CL` | `L_8M10_SED` |
| 11 | `M11` | `M11H` | `630_M11` | `8M11CL` | `L_8M11_SED` |
| 12 | `M12` | `M12H` | `640_M12` | `8M12CL` | `L_8M12_SED` |
| 13 | `M13` | `M13H` | `650_M13` | `8M13CL` | `L_8M13_SED` |
| 14 | `M14` | `M14H` | `660_M14` | `8M14CL` | `L_8M14_SED` |
| BM0 | `BM0` | `BM0H` | `820_BM0` | *(n/a)*  | `L_8BM0_SED` |

**Naming rule (LAYER column):**
- n ≤ 9  → `MT{n}`   (e.g. MT5, MT6 … MT9)
- n ≥ 10 → `M{n}`    (e.g. M10, M11 … M14)
- BM0    → `BM0`

**Defect layer token rule (confirmed for MT5, MT6 — infer for others):**
- `8M{n}CL` → `MT{n}` for n ≤ 9
- `8M{n}CL` → `M{n}`  for n ≥ 10

---

## 2. Scope of the Defect-BOST Script

### Goal
Given an input CSV of wafers with associated **defect layer** values (`8M5CL` and/or `8M6CL` only),
query BOST YieldProcessDefinitions for the **full flow** process families on those wafers
(SIARC_DEP → CHM_DEP → SED → HM_ETCH → HM_CLN for each defect layer).

> **Confirmed scope: defect dataset contains MT5 and MT6 layers only. No downstream MT7.**  
> Both MT5 and MT6 full-flow aliases are included because either can drive defects at their respective inspection layer.

### Alias scope — MT5 and MT6 full flow

| Step | MT5 alias | MT6 alias |
|---|---|---|
| SIARC_DEP | `L_8M5_SIARC_DEP` | `L_8M6_SIARC_DEP` |
| CHM_DEP   | `L_8M5_CHM_DEP`   | `L_8M6_CHM_DEP`   |
| SED       | `L_8M5_SED`       | `L_8M6_SED`       |
| HM_ETCH   | `E_8M5_HM_ETCH`   | `E_8M6_HM_ETCH`   |
| HM_CLN    | `W_8M5_HM_CLN`    | `W_8M6_HM_CLN`    |

Total: **10 WEC aliases**.

### PROCESS_FAMILY types expected (from bost_process_family_explore.py runs)
- `EQUIP:N58_XPR5:L_8M{n}_SED` — scanner hardware eligibility flag (`NO_XPR5` / other)
- `M{layer}_OPC` or `MT{layer}_OPC` — OPC version assigned to this wafer (`POR_REV4.x` etc.)
- `DUV_M{n}_RETICLE_GVB` — reticle quality/GVB status (`GOOD` / other) — *only present for some layers*

---

## 3. Input CSV Schema (expected)

The defect workspace script should accept a CSV with at least:

| Column | Description |
|---|---|
| `LOT` | Lot ID (e.g. `D5059670`) |
| `WAFER_ID` | Wafer barcode |
| `DEFECT_LAYER` | Defect layer token (e.g. `8M5CL`, `8M6CL`) |

Optional useful columns: `DATA_COLLECTION_TIME`, `DEFECT_COUNT`, `INSPECTION_TOOL`.

---

## 4. Defect Layer → LAYER Mapping Function (Python)

```python
import re

def defect_layer_to_sdtt_layer(defect_layer: str) -> str | None:
    """
    Map defect layer token (e.g. '8M5CL', '8M10CL') to sDTT LAYER convention.
    MT{n} for n <= 9, M{n} for n >= 10.
    Returns None if no numeric layer found.
    """
    m = re.search(r'M(\d+)', str(defect_layer))
    if not m:
        return None
    n = int(m.group(1))
    return f"MT{n}" if n <= 9 else f"M{n}"

def sdtt_layer_to_bost_alias(sdtt_layer: str) -> str | None:
    """
    Map sDTT LAYER token (e.g. 'MT5', 'M10') to the BOST SED WEC alias.
    e.g. MT5 -> L_8M5_SED, M10 -> L_8M10_SED
    """
    m = re.match(r'M[T]?(\d+)$', str(sdtt_layer))
    if not m:
        return None
    n = m.group(1)    # e.g. '5', '10'
    return f"L_8M{n}_SED"
```

---

## 5. Script Design (for defect workspace)

### Inputs
- `INPUT_CSV`: path to defect wafer CSV (LOT, WAFER_ID, DEFECT_LAYER)
- `DSN`: Oracle DSN (same as sDTT workspace: `D1D_PROD_XEUS_GAJT`)

### Phase A — Build layer scope
Defect scope is fixed to MT5 and MT6 full flow, so the alias lists are hardcoded:
```python
WEC_ALIASES = [
    "L_8M5_SIARC_DEP", "L_8M5_CHM_DEP", "L_8M5_SED", "E_8M5_HM_ETCH", "W_8M5_HM_CLN",
    "L_8M6_SIARC_DEP", "L_8M6_CHM_DEP", "L_8M6_SED", "E_8M6_HM_ETCH", "W_8M6_HM_CLN",
]
CD_ALIASES = ["L_8M5_DCCD", "L_8M6_DCCD"]
```
1. Load input CSV, extract unique LOT + WAFER_ID pairs.
2. Apply chunked IN-clause guard (ORA-01795) on both LOT and WAFER lists.

### Phase B — BOST query
- Reuse `_chunked_in_clause()` for LOT + WAFER_ID IN-lists (ORA-01795 guard).
- TRIGGER_OPERATION filter: `INSTR(f.TRIGGER_OPERATION, 'L_8M{n}_SED') > 0` for each alias.
- Same join pattern as `bost_process_family_explore.py` (B_META_WAFER_FAB → B_WAFER_PROCESS_DEFN → B_CFG_PROCESS_DEFN_FAMILY → B_CFG_PROCESS_DEFN).

### Phase C — Output enrichment
- Add `LAYER` column from TRIGGER_OPERATION (using `_trig_to_layer()` logic above).
- Left-join result back to input CSV on LOT + WAFER_ID so each defect row gets its BOST columns.
- Save to `bost_defect_enriched_{YYYYMMDD}.csv`.

---

## 6. Key Constants (carry over from bost_process_family_explore.py)

```python
DSN         = "D1D_PROD_XEUS_GAJT"
PROCESS     = "1278"
FAB         = "D1D"
DATA_SOURCE = "D1_P1278"
IS_LATEST   = "Y"
IS_ACTIVE   = "Y"
```

---

## 7. Open Questions / Confirm Before Coding

- [ ] Does the defect workspace use the same DSN (`D1D_PROD_XEUS_GAJT`) or a different one?
- [ ] Should the output join preserve all defect CSV columns, or just LOT/WAFER_ID/DEFECT_LAYER + BOST columns?
- [ ] Confirm exact column names in the defect input CSV (LOT, WAFER_ID, DEFECT_LAYER — or different?).
