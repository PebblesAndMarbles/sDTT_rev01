# sDTT FLEET Script — Build Plan

**Purpose:** A stripped-down SDTT reporting script that drops chamber/APC-specific logic and instead enables flexible fleet-wide variability analysis with user-selectable layer filtering, grouping columns, and legend columns.  No `CHAMBER` column. No `APC_B_TOOL` trend. No Bivariate trend columns.

---

## What to Remove vs. Keep vs. Replace

### Remove entirely
| Element | Why |
|---|---|
| `chamber_data` lookup table | No member-to-chamber mapping needed |
| "SELECT TEAM MEMBER" radio box panel | Replaced by layer filter panel |
| "SINGLE CHAMBER OVERRIDE" panel | Not applicable |
| `New Column("CHAMBER", ...)` logic | No fleet vs. chamber split |
| `Color or Mark by Column(:CHAMBER, ...)` | Replaced by user-chosen legend column |
| `FLEET` override to black/gray markers | Not applicable |
| `fg_trend_btool` / `APC_B_TOOL` bivariate column | Not relevant without APC context |
| `config["ycol1"]` / `vcol1` CHAMBER variability | Replaced by user-chosen groupings |
| `Format_Trend_Charts` function | No bivariate trend columns at all |
| All `fg_trend_*` FitGroup blocks | No spline/bivariate columns |
| Single-chamber `xcol==2` branching in `Format_Variability_Charts` | Only multi-chamber (fleet) mode exists |
| `val_include_sigma` / `val_show_splines` flags | Simpler scope — always show or make static |

### Keep (largely unchanged)
| Element | Notes |
|---|---|
| `path` / `path2` global paths | Same data sources |
| `val_include_f32` checkbox | Still useful |
| `val_data_type` (SPC/ALLSTATS) radio box | Still useful |
| `val_cb_close_reports_and_windows` checkbox | Still useful |
| Limit-loading loop (unique_layers → AA) | Identical |
| `Format_Variability_Charts` function | Simplify: remove `xcol==2` branch |
| Distribution FitGroup (info box) | Keep as-is |
| Data filter (time axis) | Keep |

### Mutate / replace
| Element | What it becomes |
|---|---|
| Chamber selection → **Layer selection** | See section below |
| `config["vcol1"]` fixed to `"CHAMBER"` | Driven by user-chosen grouping column(s) |
| `config["legendcol1"]` fixed to `"CHAMBER"` | Driven by user-chosen legend column |
| Single H List Box of fixed columns | Dynamic list based on dialog selections |

---

## Dialog Design

### Panel: LAYER SELECTION
The user selects which `WEC_LAYER` values to include before the report is generated.

**Recommended approach — three-way radio selection:**
```
Radio Box({"All Layers", "Individual Layer", "Custom (comma-separated)"})

  If "Individual Layer":
    → populate a List Box with unique WEC_LAYER values from the data file at dialog open time
      (Open file, get unique layers, close file, display in list)

  If "Custom":
    → Text Edit Box for manual comma-separated input (same Trim/Words pattern as SUBENTITY override)
```

**Implementation note:** At dialog open, do a lightweight pre-read:
```jsl
dt_pre = Open(path);
available_layers = (Associative Array(dt_pre:WEC_LAYER) << Get Keys);
dt_pre << Close Window;
// populate layer_lb = List Box(available_layers, ...)
```
This gives you a populated list without the user needing to type layer names.

After "Generate Report" is clicked, resolve `selected_layers` as a list (or empty = all).  
Then before charting: `dt << Select Where(!(Contains(selected_layers, :WEC_LAYER))); dt << Delete Rows;`  
(or keep a row state filter approach if you prefer non-destructive).

---

### Panel: VARIABILITY CHART GROUPINGS
Define which columns to use as X-axis groupings for each variability chart column.

**Planned grouping options** *(to be defined in more detail by user)*:
- `CHAMBER` / `SUBENTITY`
- `PRODUCT_GROUP`
- `PROD_MOP_PILOT`
- `WEC_RECIPE`
- `LOT_TYPE`
- `ROUTE_TYPE`

**Recommended dialog implementation:**
```
// Multiple Check Boxes, one per candidate grouping column
cb_grp_chamber    = Check Box("CHAMBER / SUBENTITY")
cb_grp_product    = Check Box("PRODUCT GROUP")
cb_grp_mop        = Check Box("PROD / MOP / PILOT")
cb_grp_recipe     = Check Box("RECIPE")
cb_grp_lottype    = Check Box("LOT TYPE")
```
Each checked grouping generates one variability chart column in the H List Box.  
Build a list `active_vcols = {}` from the checked boxes, then loop:
```jsl
For Each({vcol}, active_vcols,
    fg = dt << FitGroup(
        vc = dt << Variability Chart(Y(...), X(Column(vcol)), By(Column(bycol1)), ...),
        SendToReport(...)
    );
    Format_Variability_Charts(vc, stat, type, 1, config, vcol);
);
```

---

### Panel: LEGEND COLUMN
Single Radio Box (or dropdown) to choose what column colors/groups the scatter points:
```
Radio Box({"CHAMBER", "PRODUCT_GROUP", "ROUTE_TYPE", "LOT_TYPE", "IS_POR"})
```
This sets `legendcol1` used in `Format_Variability_Charts` and replaces the hardcoded `"CHAMBER"` in `config`.

---

### Panel: OPTIONS (same as current)
- Close Reports & Windows
- Include F32 Data
- Include Sigma Charts *(carry over from PILOT 2)*

---

### Panel: DATA TYPE (same as current)
- SPC / ALLSTATS radio

---

## `ProcessFLEETData` Function Signature (proposed)
```jsl
ProcessFLEETData = Function(
  {val_cb_close_reports_and_windows, val_data_type, selected_layers,
   val_include_f32, val_include_sigma, active_vcols, legendcol1},
  ...
);
```

---

## `Format_Variability_Charts` Simplification
Remove the `xcol == 2` branch entirely — there is no single-chamber two-X-column mode in fleet context.  
The function signature can drop the `xcol` parameter:
```jsl
Format_Variability_Charts = Function({var_chart_object, stat, type, config, vcol}, ...)
```
The `prod_mop_pilot_height` / `chvfl_height` branching on vcol name can remain for axis height sizing.

---

## Suggested Output Layout (H List Box columns, left to right)
For each MEAN stat, then optionally SIGMA:

| Col | Chart | Grouping |
|---|---|---|
| 1 | MEAN DTT Variability | CHAMBER (if checked) |
| 2 | MEAN DTT Variability | PRODUCT GROUP (if checked) |
| 3 | MEAN DTT Variability | PROD/MOP/PILOT (if checked) |
| ... | ... | (each active grouping) |
| n | MEAN TARGET Variability | same loop |
| n+1 | SIGMA DTT Variability | same loop *(if sigma checkbox on)* |
| n+k | SIGMA TARGET Variability | same loop *(if sigma checkbox on)* |

This generates a fully dynamic column count based on dialog selections.

---

## File Naming Suggestion
`sDTT Rev04 - FLEET.jsl`

---

## Things to Confirm Before Building
1. Desired behavior when layer filter is applied — destructive row delete vs. row-state hide (Hide & Exclude)?  Row delete is simpler and consistent with current SPC/ALLSTATS pattern. Row-state hide is non-destructive but slightly more complex.
2. Exact candidate list for GROUPING and LEGEND columns — confirm column names available in both D1V and F32 datasets.
3. Should the FLEET script display a DISTRIBUTIONS info box (FitGroup of Nominal Distributions)? Currently it's large — a collapsed-by-default option may be preferred.
4. Should `val_show_splines` even exist in the FLEET version? If there are no bivariates, there are no splines — the flag is irrelevant and should be omitted.
