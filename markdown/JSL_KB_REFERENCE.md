# JSL Knowledge Base Reference (sDTT)

This file links sDTT JSL scripts to the centralized JSL_KB.

## Central KB Location
\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\JSL_KB\

## Relevant Sections
- [FLEET JSL Logic](../../../../JSL_KB/FLEET.md)
- [DISPO JSL Logic](../../../../JSL_KB/DISPO.md)
- [PROD TARGETING JSL Logic](../../../../JSL_KB/PROD_TARGETING.md)

## Local Notes
- Chamber Dispo (`AMEct sDTT HCCD Chamber Dispo.jsl`):
	- For dataset 1278, default source is 60-day APC CSV.
	- `Include Full History` switches source to full APC CSV.
	- Dataset selector is consolidated (`1278 D1V`, `1278 F32`, `1280 D1V`).
	- `SUBENTITY FORM OVERRIDE` uses tabbed search (`D1V`, `F32`) and merges selections from both tabs.
	- If no tab selections exist, fallback order is: full SUBENTITY text list, then single chamber (`AME4` + `ENTITY` + `_PM`), then Tool Owner list.
- Layer Dispo (`AMEct sDTT HCCD Layer Dispo.jsl`):
	- For technology 1278, default source is 60-day APC CSV(s).
	- `Include Full History` switches 1278 source to full APC CSV(s).
	- Internal 45-day row deletion is retained only for technology 1280.
- Scope note: 1280 source behavior is intentionally unchanged in this update.

### Minimal Working Example: Tabbed SUBENTITY Search (Chamber Dispo)

```jsl
// 1) Build two source lists (D1V and F32) and current visible lists.
subentity_fleet_d1v = Build_Subentity_Fleet_List();
subentity_fleet_f32 = {"AME801_PM1", "AME801_PM2", "..."};
subentity_picker_visible_d1v = subentity_fleet_d1v;
subentity_picker_visible_f32 = subentity_fleet_f32;

// 2) Add a Tab Box in the dialog, each tab with Search + List Box.
subentity_tab_box = Tab Box(
	"D1V",
	V List Box(
		H List Box(
			SUBENTITY_FILTER_TB_D1V = Text Edit Box(""),
			Button Box("Search",
				filter_text = SUBENTITY_FILTER_TB_D1V << Get Text;
				subentity_picker_visible_d1v = Filter_Subentity_Fleet_List(subentity_fleet_d1v, filter_text);
				subentity_picker_lb_d1v << Set Items(subentity_picker_visible_d1v);
			)
		),
		subentity_picker_lb_d1v = List Box(subentity_picker_visible_d1v, Max Selected(200))
	),
	"F32",
	V List Box(
		H List Box(
			SUBENTITY_FILTER_TB_F32 = Text Edit Box(""),
			Button Box("Search",
				filter_text = SUBENTITY_FILTER_TB_F32 << Get Text;
				subentity_picker_visible_f32 = Filter_Subentity_Fleet_List(subentity_fleet_f32, filter_text);
				subentity_picker_lb_f32 << Set Items(subentity_picker_visible_f32);
			)
		),
		subentity_picker_lb_f32 = List Box(subentity_picker_visible_f32, Max Selected(200))
	)
);

// 3) On Generate Report: merge selected entries from both tabs.
picker_d1v = Normalize_Subentity_Selection(subentity_picker_lb_d1v << Get Selected, subentity_picker_visible_d1v);
picker_f32 = Normalize_Subentity_Selection(subentity_picker_lb_f32 << Get Selected, subentity_picker_visible_f32);
picker_all = picker_d1v;
For(i = 1, i <= N Items(picker_f32), i++, Insert Into(picker_all, picker_f32[i]));
picker_all = Associative Array(picker_all) << Get Keys;
Sort List(picker_all);

If(N Items(picker_all) > 0,
	chamber_list = picker_all;
, 
	// fallback to text list / single chamber / owner list
);
```

Notes:
- Keep search filtering list-local: D1V search updates only D1V list, F32 search updates only F32 list.
- Keep selection normalization centralized (`Normalize_Subentity_Selection`) to handle list or index returns consistently.
- `CHAMBER` derivation remains unchanged: `If(Contains(chamber_list, :SUBENTITY), :SUBENTITY, "FLEET")`.

---

---

### Target Regime Filter (`Apply_Target_Regime_Filter`)

**Purpose:** Retains only rows belonging to the most recent continuous target regime for each `PROD_MOP_PILOT` group. Useful for isolating current-regime data when targets have changed over time.

**Defined in:**
- `AMEct sDTT HCCD Chamber Dispo.jsl` — immediately after `Debug_Trace` definition (~line 119)
- `AMEct sDTT HCCD Layer Dispo.jsl` — immediately after `Parse_Pilot_List` definition (~line 196)

**Required columns:** `PROD_MOP_PILOT`, `DATA_COLLECTION_TIME`, `ALLSTATS_MEAN_TARGET_VALUE`

**Group key:** `PROD_MOP_PILOT` only — targets are specified at this granularity, and `PROD_MOP_PILOT` values are unique across layers, so no `WEC_LAYER` component is needed.

**Logic:**
1. Sort `dt` by `PROD_MOP_PILOT`, `DATA_COLLECTION_TIME` (ascending both).
2. Create/reset temp columns `TARGET_GROUP_KEY` and `TARGET_REGIME_LATEST` on `dt`.
3. For each unique `PROD_MOP_PILOT`: walk backwards from the last row finding the contiguous streak where `ALLSTATS_MEAN_TARGET_VALUE` equals the last row's value; mark those rows `TARGET_REGIME_LATEST=1`. If the last row's target is missing, keep all rows for that group.
4. Delete all rows where `TARGET_REGIME_LATEST != 1`.

**Temp columns left on table:** `TARGET_GROUP_KEY` (Character), `TARGET_REGIME_LATEST` (Numeric). These are working columns only.

**Placement in pipeline:** After SPC/dynwafer DATA TYPE filter, before APC MODEL filter — runs early so subsequent filters operate on the already-pruned set.

**Dialog control:** `cb_current_target_only = Check Box("Current Target Only")` in the OPTIONS panel (unchecked by default). Handler reads `val_current_target_only = cb_current_target_only << Get;`. Passed as last parameter to `ProcessSDTTData` / `ProcessFLEETData`.

**Enabling guard:**
```jsl
If( val_current_target_only == 1,
    Apply_Target_Regime_Filter( dt );
);
```

_Last updated: May 27, 2026_