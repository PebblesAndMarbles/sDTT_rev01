# Variability Chart Vertical Sizing — Reference Pattern

**Problem solved:** Variability charts in a side-by-side H List Box layout were stretching vertically to match the full height of the GroupBy Row Legend on the adjacent trend chart (could reach 700–900 px with large SUBENTITY / PROD_MOP_PILOT legend).

---

## Layout Context

Trend chart and variability chart live in the same `H List Box` row, repeated per By-group (e.g., WEC_LAYER). A post-render alignment function runs after `New Window` to equalize row heights.

```
H List Box(
    fg_trend_dtt_mean,     // Bivariate + GroupBy legend
    fg_var_dtt_mean_user,  // Variability Chart
)
```

---

## Solution (3 parts)

### 1. Format_Variability_Charts — frame height baseline

Set the var chart FrameBox height using this formula, subtracting an empirical **-100 px** offset so the chart body is intentionally shorter than the trend chart for most rows:

```jsl
frame_height = rowFBheight + trend_axisbox2_height_target - axisbox2_height - 100;
frame_height = Max( 80, frame_height );
Try( framebox << Set Height( frame_height ) );
```

- `rowFBheight` — trend chart FrameBox height (e.g., 370 px)
- `trend_axisbox2_height_target` — measured height of the trend chart's time-axis label area (~18–60 px)
- `axisbox2_height` — measured height of the var chart's X-axis label area (depends on label length / angle)
- Wrap in `Try()` — matches Surf Scan Fleet Dispo pattern, prevents silent failures

Also wrap all other `Set Height` / `Set Width` calls in `Format_Variability_Charts` in `Try()`.

---

### 2. Apply_Target_Report_Height — spacer-only padding (NO FrameBox growth)

Post-render, this function is called per row to pad the var chart up to match the trend chart's total height. **Do not grow the FrameBox** — use a Spacer Box only (matches `AMEct 1278 Surf Scan Fleet Dispo.jsl`):

```jsl
Apply_Target_Report_Height = Function( {rpt, target_h, stat_label, row_label},
    If( Is Missing( target_h ), Return() );
    _current_h = Get_Report_Height( rpt );
    If( Is Missing( _current_h ), Return() );

    If( _current_h < target_h,
        _delta_h = target_h - _current_h;
        Try( rpt << Set Height( target_h ) );
        Try( rpt << Append( Spacer Box( Size( 1, _delta_h ) ) ) );
    ,
        If( _current_h > target_h,
            Try( rpt << Set Height( target_h ) );
        );
    );
);
```

Key: `Spacer Box( Size( 1, _delta_h ) )` absorbs leftover height below the chart. The chart data area stays compact.

---

### 3. Align_ByGroup_Row_Heights — use full trend report height as target

Use `Get_Report_Height( trend_rpt )` (full rendered height, **including** the GroupBy Row Legend) as the alignment target. This ensures the spacer fills the right amount for tall-legend rows:

```jsl
Try( _th = Get_Report_Height( _tr ), _th = . );
```

Do **not** use `Get_FrameBox_Height` here — that only returns the chart body and produces under-padded var charts when the legend is large.

---

## Combined Effect

| Situation | Var chart behavior |
|---|---|
| Short legend / no legend | Frame set to `rowFBheight - ~100 px`; spacer pads up to match trend total height |
| Tall legend (e.g., 30+ SUBENTITY values) | Frame stays compact; large spacer fills the gap to match trend total height |
| All rows | Chart data area height is consistent; no stretching of the plotting frame |

---

## Reference Files

- Pattern implemented in: `AMEct sDTT HCCD Layer Dispo.jsl`
- Surf Scan reference (original source of spacer-only approach): `\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\Defects\BE\AMEct 1278 Surf Scan Fleet Dispo.jsl`
