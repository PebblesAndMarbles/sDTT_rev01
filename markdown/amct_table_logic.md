# AMCT Table Logic

Last updated: 2026-05-13

This note summarizes the logic used by the JMP/JSL add-in to generate AMCT-style target tables and the related pull path that feeds them.

## What the add-in is doing

The add-in does not read an existing AMCT target workbook as its main input. Instead it:

1. Pulls a restricted VMR/BigCsv source for a selected process and workweek.
2. Derives RS and RAW parameter files from the `BASESHARE` path in that BigCsv table.
3. Opens the derived `*_rs.csv.gz` and `*_raw.csv.gz` files.
4. Cleans and reshapes the data into a DTT clustering table.
5. Computes new target values by cluster.
6. Builds an AMCT-style output table and saves an Excel workbook named like `ALL.Lithography.CD.CDSEM.MFG.CDSEM_TARGETS_<process>.xlsx`.

## Source pull logic

### Folder selection

The restricted source root is constructed from the selected process:

- `\\rf3p-nas-D1DPCSA-office.rf3prod.mfg.intel.com\D1DPCSA\HR\Restricted\P<process>\<process>_MAIN_VMR`

For your example run:

- process = `1278`
- workweek = `202618`

The source file becomes:

- `\\rf3p-nas-D1DPCSA-office.rf3prod.mfg.intel.com\D1DPCSA\HR\Restricted\P1278\1278_MAIN_VMR\202618\1278__BigCsv.txt`

### Chart-selection filter fields

The add-in lets the user filter chart candidates by:

- `MEASURED_LAYER`
- `MOP`
- `STRUCTURE`
- `CD_TERMS`
- `STATISTIC`

Your example selection:

- `MEASURED_LAYER = MT9H`
- `MOP = *`
- `STRUCTURE = NEST`
- `CD_TERMS = MEAN_DTT`
- `STATISTIC = XBAR`

These values are parsed from `Parameter Name` in the BigCsv table and used to narrow the candidate rows before the RS/RAW files are opened.

### Derived file paths

For each selected BigCsv row, the add-in derives the related parameter files from `BASESHARE`:

- `...\ParamDetails\<basename>_rs.csv`
- `...\ParamDetails\<basename>_raw.csv`
- then appends `.gz` if needed

So the actual opened files are typically:

- `..._rs.csv.gz`
- `..._raw.csv.gz`

## Data normalization before target creation

After opening the RS/RAW data, the scripts normalize the source columns used by the AMCT table.

### Common fields extracted from `CATEGORY_VALUE`

The scripts build these columns from string parsing / regex extraction:

- `PILOT_NAME`
- `SPC_MEASURED_LAYER`
- `SPC_CURRENT_LAYER` or `SPC_STRUCTURE` depending on script variant
- `LAYER`
- `REV` in the non-FCCD path
- `_PROCESS` from the route/process token

### Row filtering

The pull tables are reduced before clustering using a combination of these filters:

- keep only the chosen facility / site row
- drop rows with missing values where the script expects usable measurements
- remove rows not matching the chosen run statistic (`MEAN`, `SIGMA`, etc.)
- in the FCCD puller, keep only:
  - `FACILITY == 'RA3'`
  - `Grouping Item == 'ALL'`
  - `Parameter Name` containing `FCCD`

## Clustering and target synthesis

The target table is not copied from the input file. It is synthesized from clustering.

### Group-by controls

The user chooses the grouping columns used to build `DTTby`.

Observed options include:

- `PRODUCT`
- `PRODUCT_REDUX`
- `OPERATION`
- `REV` or `PILOT_NAME` depending on script variant

`DTTby` is built by concatenating the selected columns with underscores.

### Response and group roles

The user also assigns:

- one numeric response column
- one character grouping column

Those roles feed the hierarchical clustering step.

### Cluster calculation

The scripts:

- run hierarchical clustering on the selected response column
- build a cluster count / distance diagnostic
- choose a cluster count from a second-derivative heuristic on the cluster-distance curve
- optionally smooth the curve to pick a more stable elbow

### New target calculation

After clustering, the scripts compute:

- `New_TGT = mean(value) by cluster`
- `zn_sgm = std dev(value) by cluster`

Then a per-row delta is added:

- `New_DTT = val - New_TGT`

This is the core target-generation step.

## AMCT-style output table construction

The final AMCT-style table is built from a summary of the clustered join table.

### Canonical output columns

The table is reshaped into a JMP summary table with fields such as:

- `T`
- `RowID`
- `ROW_ORDER`
- `PILOT_NAME`
- `PRODUCT`
- `LAYER`
- `OPERATION`
- `SPC_MEASURED_LAYER`
- `SPC_STRUCTURE`
- `TARGET`
- `SCALE` or omitted for non-mean runs
- `_PROCESS`
- `COMMENTS`
- `ACTION_ON_WP`

### Rename and cleanup

The script then:

- renames `PRODUCT_AM` to `PRODUCT`
- renames `New_TGT` to `TARGET`
- rounds `TARGET` to 2 decimals
- drops helper columns like `N Rows`, `N Rows 2`, and `UserDefined_Mean_TGT`
- removes `SCALE` for non-`MEAN` runs
- replaces `POR` with `*` in `PILOT_NAME`

## PRODUCT spacing logic for AMCT compatibility

This is the key formatting rule that explains the spaces you saw in the exported Excel file.

The add-in creates `PRODUCT_AM` with this logic:

- take the first 6 characters of `PRODUCT`
- add a space
- then format the remaining characters one-by-one with embedded spaces depending on suffix length

Observed behavior:

- 1 trailing character -> base + three-space style suffix, e.g. `1L78EV   A`
- 2 trailing characters -> base + two letters separated by a space, e.g. `1L78EV A A`
- 3 trailing characters -> base + three spaced suffix characters
- 4 trailing characters -> base + four spaced suffix characters

In JSL form, the logic is:

```text
Substr(:PRODUCT, 1, 6) || " " ||
If(
  Length(Substr(:PRODUCT, 7, 20)) == 1, "  " || Substr(:PRODUCT, 7, 1),
  Length(Substr(:PRODUCT, 7, 20)) == 2, Substr(:PRODUCT, 7, 1) || " " || Substr(:PRODUCT, 8, 1),
  Length(Substr(:PRODUCT, 7, 20)) == 3, Substr(:PRODUCT, 7, 1) || " " || Substr(:PRODUCT, 8, 1) || " " || Substr(:PRODUCT, 9, 1),
  Length(Substr(:PRODUCT, 7, 20)) == 4, Substr(:PRODUCT, 7, 1) || " " || Substr(:PRODUCT, 8, 1) || " " || Substr(:PRODUCT, 9, 1) || " " || Substr(:PRODUCT, 10, 1)
)
```

This formatting is applied right before the summary table is finalized, so the spaces are inherited by the XLSX output.

## Output filenames and shares

The add-in saves the generated workbook to the DTT share root under a user/date-specific folder:

- `\\rf3p-nas-D1DPCSA-office.rf3prod.mfg.intel.com\D1DPCSA\DTT_WP\<user>_<ceid>_<test>_<date>\`

Main workbook name:

- `ALL.Lithography.CD.CDSEM.MFG.CDSEM_TARGETS_<process>.xlsx`

Also written:

- journal `.jrn`
- limit file derivative workbook

The scripts also open:

- `\\rf3p-nas-D1DPCSA-office.rf3prod.mfg.intel.com\D1DPCSA\DTT_WP\Admin\LimitFile.jmp`

## FCCD vs non-FCCD variants

The FCCD and non-FCCD versions are very similar, but the non-FCCD path adds more run-stat options and slightly different column extraction.

### FCCD variant

- focuses on `MEAN` / `SIGMA`
- uses `CURRENT_LAYER`, `SPC_MEASURED_LAYER`, `SPC_STRUCTURE`, and `PILOT_NAME`
- produces the same AMCT-style export pattern

### Non-FCCD variant

- supports additional run-stat choices such as `RANGE` and `MAX`
- builds `REV`
- can derive a `TARGETS` string like `C10 (DTT_CONSTANT)=<value>` for the AM table

## Practical implication for the sDTT Python pipeline

If you want your Python pipeline to match the add-in output, the important compatibility steps are:

1. Preserve the same source filters.
2. Build the same cluster-driven `New_TGT` logic.
3. Rename `PRODUCT_AM` to `PRODUCT` only after applying the spacing transform.
4. Apply the same `PILOT_NAME` cleanup (`POR` -> `*`).
5. Keep the final output naming and column order aligned with the add-in.

## Bottom line

The AMCT-style Excel workbook is generated from clustered DTT data, not from a pre-existing target sheet. The special spacing in `PRODUCT` is introduced deliberately by the JSL export step, and that formatting is the reason the spreadsheet looks different from the compact product strings in the upstream Python pipeline.