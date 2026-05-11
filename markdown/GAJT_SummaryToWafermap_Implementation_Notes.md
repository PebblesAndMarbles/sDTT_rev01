# GAJT Pattern: Summary Point → Raw Within-Wafer Wafermap Sync
**Reference for: defect summarization project + future sDTT raw measurement wafermap**
*Researched March 2026 from GAJT addin at:*
`C:\Users\tbatson\AppData\Roaming\SAS\JMP\AddIns\gajtv.intel.com\`

---

## 1. What This Does (User-Facing Behavior)

When a user clicks/selects a row in a summary trend chart or variability chart (one row = one wafer or lot summary metric), the raw within-wafer coordinate data (X/Y defect positions, or measurement site positions) for that wafer automatically populates/updates a `Bivariate(Y(:Y), X(:X))` wafermap panel in the same window.

The source data has this structure before processing:
- **Summary table** (one row per wafer): comma-separated lists of X coords, Y coords, defect IDs, etc. stored as character strings within each cell
- **Example column values:**
  - `WAFER_X`: `-134.843765,25.519898,102.687796,-27.708695,...`
  - `WAFER_Y`: `15.622416,-126.879796,-70.599484,82.22082,...`
  - `DEFECT_ID`: `20,33,37,63,82,89,...`

After processing, an unfolded child table has one row per defect point, which feeds the live wafermap.

---

## 2. Key GAJT Files and Their Roles

| File | Role |
|---|---|
| `Classes/Tables/Table/UnfoldTable.jsl` | **Core engine** — splits comma-separated column values into individual rows. Generic, reusable. |
| `Classes/UI/Forms/GetData/DefectData/UnfoldRawDefectData.jsl` | **Defect-specific wrapper** around UnfoldTable. Defines which columns are "rep" (replicated) vs "cat" (comma-split), retypes numeric cols, builds DEFECT_KEY. |
| `Classes/UI/Forms/GetData/DefectData/BOLT/BOLTGraphsBase.jsl` | **Base class** for all BOLT graph windows. Contains `MakeRowStateHandler`, `PropagateRowStates`, `UpdateRowVisibility`. The delta-guard logic lives here. |
| `Classes/UI/Forms/GetData/DefectData/BOLT/BOLTGraphsScan.jsl` | **Concrete implementation** for scan/surfscan data. Shows how `MakeRowStateHandler` is wired at startup and how `rowselectiontable` links summary → coord table. |
| `Classes/UI/Forms/GetData/DefectData/BOLT/BOLTEBeamWaferMapGallery.jsl` | Wafermap gallery that renders the `Bivariate` plot and the `gajtcx:execute(DISPLAY_IMAGES)` click-through handler. |
| `Classes/UI/Forms/GetData/DefectData/BOLT/BOLTGraphsAPIImages.jsl` | Builds the image viewer window when a wafermap point is clicked (uses `DEFECT_KEY` to fetch image). Only needed if you want click-through to images. |
| `Namespaces/cx.jsl` | `gajtcx:Execute` — the context/namespace dispatch mechanism used to pass `DEFECT_KEY` from the graphics script click handler back into the GAJT form object. |

---

## 3. The Three-Layer Architecture

```
LAYER 1: Summary Table (dt_summary)
  One row per wafer/lot.
  Columns contain comma-separated raw measurement lists.
  This is what the trend chart / variability chart plots.

        ↕  MakeRowStateHandler fires on any row selection change

LAYER 2: Unfolded Coordinate Table (dt_coords)
  One row per defect / measurement site.
  Created by UnfoldTable from the selected summary row(s).
  Column ORIGINAL_ROW_NUMBER links back to summary table row index.
  Key join columns: SITE, WAFER_KEY, INSPECTION_TIME (or equivalent).

        ↕  UpdateRowVisibility propagates row states from summary → coord table

LAYER 3: Bivariate Wafermap
  Bivariate(Y(:WAFER_Y), X(:WAFER_X)) on dt_coords
  Filtered/redrawn as coord table row states change.
  Optional: gajtcx:execute click handler to trigger image display.
```

---

## 4. Step-by-Step Implementation Pattern

### Step 4.1 — Build the Unfolded Coordinate Table

GAJT uses `UnfoldTable` (generic) wrapped by `UnfoldRawDefectData` (defect-specific).
For a custom implementation, the equivalent pure JSL is:

```jsl
// Inputs:
//   summary_dt  = the summary data table (one row per wafer, comma-list cols)
//   selected_rows = row indices from the summary table that are selected
//   rep_cols = {"SITE", "WAFER_KEY", "INSPECTION_TIME"}   // replicated 1:1 per defect
//   cat_cols = {"DEFECT_ID", "WAFER_X", "WAFER_Y"}        // the comma-split cols
//   delim    = ","

coord_dt = New Table("CoordTable", Invisible);

// For each cat_col, split comma strings from selected rows into a flat list
n_vals = .;
For(c = 1, c <= N Items(cat_cols), c++,
    col = Column(summary_dt, cat_cols[c]);
    // Concatenate all selected rows' comma-strings into one master string, then split
    flat_vals = Words(
        Concat Items(col[selected_rows], delim),
        delim
    );
    nv = N Items(flat_vals);
    If(Is Missing(n_vals), n_vals = nv);
    coord_dt << New Column(cat_cols[c], Character, Nominal, Values(Eval(flat_vals)));
);

// Build the back-index: which summary row does each coord row come from?
rowidxs = J(1, n_vals, 0);
prev = 0;
ref_col = Column(summary_dt, cat_cols[1]);
For(i = 1, i <= N Items(selected_rows), i++,
    r = selected_rows[i];
    nv = N Items(Words(ref_col[r], delim));
    If(nv,
        next = prev + nv;
        rowidxs[(prev+1)::next] = r;
        prev = next;
    );
);

// Replicate the "rep_cols" values across coord rows using the back-index
For(c = 1, c <= N Items(rep_cols), c++,
    col = Column(summary_dt, rep_cols[c]);
    vals = col[rowidxs];   // JMP matrix index auto-replicates
    Column(coord_dt, rep_cols[c]) << Set Values(Eval(vals));
);

// Add ORIGINAL_ROW_NUMBER (useful for re-joining to summary table later)
coord_dt << New Column("ORIGINAL_ROW_NUMBER", Numeric, Continuous,
    Values(Eval(rowidxs))
);

// Retype numeric columns
For(c = 1, c <= N Items(cat_cols), c++,
    Column(coord_dt, cat_cols[c]) << Data Type("Numeric") << Modeling Type("Continuous");
);

// Build a composite key (equivalent to GAJT's DEFECT_KEY)
coord_dt << New Column("POINT_KEY", Character, Nominal, Set Each Value(
    :SITE[] || "_" || Char(:WAFER_KEY[]) || "_" || Char(:DEFECT_ID[])
));
```

### Step 4.2 — Attach `MakeRowStateHandler` to the Summary Table

This is the **core JMP API** that makes the sync reactive. It fires whenever any row state (selected/excluded/hidden) changes on `summary_dt`.

```jsl
// Store prev-state matrices for delta guard
prev_selected = [];
prev_excluded = [];
prev_hidden   = [];

rsh = summary_dt << Make Row State Handler(
    Function({rows}, {Default Local},
        sel = summary_dt << Get Selected Rows;
        exc = summary_dt << Get Excluded Rows;
        hid = summary_dt << Get Hidden Rows;

        // Delta guard — skip if nothing actually changed
        If(
            N Row(sel) == N Row(prev_selected) & All(sel == prev_selected) &
            N Row(exc) == N Row(prev_excluded) & All(exc == prev_excluded) &
            N Row(hid) == N Row(prev_hidden),
            Return()
        );
        prev_selected = sel;
        prev_excluded = exc;
        prev_hidden   = hid;

        // React to the new selection — rebuild coord table and update wafermap
        update_wafermap(sel);   // your function here
    )
);
```

**Key notes on `MakeRowStateHandler`:**
- Returns a handle (`rsh`). Keep a reference to it; the handler is deleted when the handle goes out of scope or when you call `summary_dt << Remove Row State Handler(rsh)`.
- Fires on *any* row state change: clicks in the trend chart, clicks in the data table, `SelectRows` called from JSL, row exclusion, row hiding — all trigger it.
- Does NOT fire on column value changes.
- The `rows` argument passed to the function is currently undocumented / unreliable; use `dt << Get Selected Rows` directly instead.

### Step 4.3 — The `update_wafermap` Reaction Function

```jsl
update_wafermap = Function({selected_rows},
    // If nothing selected, show all coords (or clear wafermap)
    If(N Row(selected_rows) == 0,
        selected_rows = (1 :: N Row(summary_dt))`;
    );

    // Rebuild coord table for the selected summary rows
    If(Is Table(coord_dt) & Is Scriptable(coord_dt),
        coord_dt << Close(No Save);  // close old one
    );
    coord_dt = build_coord_table(summary_dt, selected_rows);   // Step 4.1 logic

    // Refresh the Bivariate wafermap in the existing window
    wafermap_report << Close Window;
    wafermap_report = coord_dt << Bivariate(
        Y(Column("WAFER_Y")),
        X(Column("WAFER_X")),
        // ... your SendToReport formatting ...
    );
);
```

**Alternative (lighter) approach — filter instead of rebuild:**
If the coord table is pre-built for ALL rows at startup, use `UpdateRowVisibility` pattern:
reproduce the summary table's row states on the coord table using `ORIGINAL_ROW_NUMBER` as the join key. GAJT does this in `BOLTGraphsScan:PropagateRowStates`.

```jsl
// Pre-built coord_dt has ORIGINAL_ROW_NUMBER column
// When summary row R is selected, select all coord rows where ORIGINAL_ROW_NUMBER == R
coord_dt << Select Where(
    Contains(Matrix(selected_rows), :ORIGINAL_ROW_NUMBER)
);
// The Bivariate auto-updates if it is linked to coord_dt row states
```

### Step 4.4 — The Wafermap `Bivariate` with Optional Click-Through

GAJT's wafermap script (from the script you originally shared):

```jsl
Bivariate(
    Y( :WAFER_Y ),
    X( :WAFER_X ),
    SendToReport(
        Dispatch({}, "Bivariate Fit of Y By X", OutlineBox,
            {Set Title("Surfscan wafermap")}
        ),
        Dispatch({}, "X", ScaleBox,
            {Min(-165), Max(165), Inc(50), Minor Ticks(1),
             Label Row({Show Major Labels(0), Show Major Ticks(0), Show Minor Ticks(0)})}
        ),
        Dispatch({}, "Y", ScaleBox,
            {Min(-165), Max(165), Inc(50), Minor Ticks(1),
             Label Row({Show Major Labels(0), Show Major Ticks(0), Show Minor Ticks(0)})}
        ),
        Dispatch({}, "", AxisBox, {Remove Axis Label}),
        Dispatch({}, "Bivar Plot", FrameBox, {
            Frame Size(300, 280),
            // Wafer circle overlay
            Add Graphics Script(2, Description("WaferCircle"),
                Pen Color(32);
                Oval(-150, -150, 150, 150);
            ),
            // GAJT click-through to image viewer (requires GAJT session context)
            // Only include if running inside a GAJT session:
            Add Graphics Script(5, Description(""),
                gajtcx:execute(
                    Namespace("#241"),   // GAJT session namespace — dynamic, see note below
                    {value("DEFECT_KEY"), run("DISPLAY_IMAGES")}
                )
            ),
            Row Legend(SLOT_ID, Color(1), Color Theme("JMP Default"(1)),
                Marker(1), Marker Theme("Standard"),
                Continuous Scale(0), Reverse Scale(0), Excluded Rows(0)
            )
        }),
        Dispatch({}, "", AxisBox(2), {Remove Axis Label})
    )
);
```

**On the `Namespace("#241")` reference:**
The number is the anonymous namespace ID assigned to the GAJT session object at runtime. GAJT resolves this via `gajtcx:ClearOldNamespaces` and `gajt:uiids`. For a standalone (non-GAJT) wafermap, remove the `Add Graphics Script(5, ...)` block entirely — the wafermap still works; you just lose the click-to-image behavior.

---

## 5. GAJT vs. Standalone JSL — Decision Points

| Feature | GAJT (full) | Standalone JSL |
|---|---|---|
| Comma-list → rows | `gajt:UnfoldRawDefectData` / `gajt:UnfoldTable` | Replicate with `Words(ConcatItems(...), delim)` — ~30 lines |
| Row change callback | `dt << MakeRowStateHandler(...)` | **Same API** — native JMP, no GAJT needed |
| Wafermap panel | `gajt:BOLTScanWafermapGallery` | Plain `Bivariate(Y(:WAFER_Y), X(:WAFER_X))` |
| Click-through to images | `gajtcx:execute(..., {value("DEFECT_KEY"), run("DISPLAY_IMAGES")})` | Omit, or implement custom `Add Graphics Script` |
| Session namespace binding | `gajtcx` infrastructure | Not needed for row-select sync; only needed for click-to-image |

**Minimum viable implementation (no GAJT dependency):**
1. Pre-build `coord_dt` from all summary rows at startup (UnfoldTable logic, ~30 lines).
2. Attach `MakeRowStateHandler` to `summary_dt` (~15 lines).
3. In the handler: propagate `SelectRows` / `Exclude` / `Hide` from `summary_dt` to `coord_dt` using `ORIGINAL_ROW_NUMBER`.
4. Bivariate on `coord_dt` auto-reflects the row states.

---

## 6. sDTT-Specific Notes

The sDTT integrated CSV (`1278sDTT_HCCD_D1V_APC.csv` etc.) contains per-wafer summary statistics, not raw within-wafer measurement coordinates. To implement the same wafermap pattern:

- **Raw coordinate source**: would need to come from a separate NCDD/surfscan raw data query (the BOLT query you showed in the other window), joined to sDTT lots by `SPC_LOT` or `WAFER_KEY`.
- **Join key**: `SPC_LOT` (in sDTT table) ↔ `LOT` + `WAFER` (in raw BOLT coord table).
- **Trigger**: `MakeRowStateHandler` on the sDTT summary table; on row selection, filter/re-query the coord table to the matching lot+wafer rows.
- **Feasibility**: straightforward once the raw coord CSV/query is available. The `MakeRowStateHandler` + `ORIGINAL_ROW_NUMBER` pattern applies directly.

---

## 7. Quick Reference — Key JSL APIs Used

```jsl
// Attach reactive row-state listener to a table
rsh = dt << Make Row State Handler(Function({rows}, {Default Local}, ... ));
dt << Remove Row State Handler(rsh);

// Get current row states
sel = dt << Get Selected Rows;   // returns matrix of row indices
exc = dt << Get Excluded Rows;
hid = dt << Get Hidden Rows;

// Apply row states
dt << Select Rows(Eval(sel));
dt << Select Excluded << Unexclude;
dt << Select Rows(Eval(exc)) << Exclude;
dt << Clear Select;

// Split comma-separated string → list of values (the UnfoldTable core)
vals = Words(ConcatItems(col[row_indices], ","), ",");
// vals is a list of strings; cast with Num() or retype the column for numeric

// Replicate summary values across unfolded rows using back-index matrix
rep_vals = Column(summary_dt, "LOT")[rowidxs];  // matrix indexing auto-replicates
```
