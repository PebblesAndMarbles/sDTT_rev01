"""
sDTT Flag Visualizer
====================
Generates 4-panel diagnostic PNG figures for CHAMBER_CENTERING flags.

Panels per figure (single row):
  1. STATISTICS_MEAN_DTT_VALUE time series  (control limits + window-start line)
  2. APC_B_TOOL time series                 (window-start line)
  3. Variability by LABEL                   (control limits)
  4. Variability by LABEL x PROD_MOP_PILOT  (control limits)

LABEL values (priority order for the flagged SUBENTITY):
  PRE_ADJUST  – row precedes WINDOW_START_DATE (pre-adjustment history)
  SPIKE       – unconfirmed B_TOOL step (|delta| >= threshold, not sustained)
  OUTLIER     – MAD-excluded by the centering test
  {SUBENTITY} – in-window, clean rows (= the N_WAFERS counted in the flag)
  FLEET       – all other chambers on the same WEC_LAYER

All panels use APC_FB_SUC==1 / ALLSTATS_DYNWAFER==DYNWAFER_001 /
STATISTICS_MEAN_DTT_VALUE data (same population as the flagging engine).

DEV_MODE=True restricts output to the single flag specified by
DEV_FLAG_SUBENTITY / DEV_FLAG_LAYER.  Set DEV_MODE=False to render all flags.

Output: <script_dir>/flag_images/PRIO{nn}_{SUBENTITY}_{WEC_LAYER}.png
"""

import logging
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
from scipy.interpolate import UnivariateSpline, CubicSpline

# ── Optional LOWESS smoother (statsmodels — better long-range trend like JMP) ────
try:
    from statsmodels.nonparametric.smoothers_lowess import lowess as _sm_lowess
    _LOWESS_AVAILABLE = True
except ImportError:
    _sm_lowess        = None
    _LOWESS_AVAILABLE = False

log = logging.getLogger(__name__)

# ── Import engine constants (with fallback defaults) ───────────────────────────
try:
    from sDTT_flagging_engine import B_TOOL_STEP_THRESHOLD, MIN_PERSIST_RUNS
except ImportError:
    B_TOOL_STEP_THRESHOLD = 0.3
    MIN_PERSIST_RUNS      = 1

# ── Development mode ───────────────────────────────────────────────────────────
DEV_MODE           = False          # True → only render the one flag below
DEV_FLAG_SUBENTITY = "AME409_PM6"  # SUBENTITY to target in dev mode
DEV_FLAG_LAYER     = "640_M12"     # WEC_LAYER  to target in dev mode

# ── Ad hoc mode (run this file directly) ───────────────────────────────────────
# Add (subentity, layer_short) tuples to ADHOC_COMBINATIONS and run:
#   python sDTT_flag_visualizer.py
# A combined CSV is written to integrated_output/ with SUBENTITY, Metal Layers,
# DELTA(nm) NEEDED, Current B_Tool, and New BTOOL for every combination that resolves.
# Set ADHOC_LOOKBACK_DAYS to limit analysis to recent history by DATA_COLLECTION_TIME.
# Use None or <=0 to disable lookback filtering.
ADHOC_LOOKBACK_DAYS = 45
ADHOC_COMBINATIONS = [
    ("AME409_PM2", "M08"),
    # ("AME427_PM4", "M08"),
    # ("AME409_PM6", "M09"),
    # ("AME427_PM6", "M09"),
    # ("AME419_PM6", "M09"),
    # ("AME411_PM4", "M10"),
    # ("AME409_PM2", "M10"),
]



# ── Marker/colour styles keyed by label ───────────────────────────────────────
# The flagged subentity colour is injected per-flag at render time.
_BASE_STYLES = {
    "FLEET":      dict(color="#AAAAAA", s=18,  alpha=0.35, zorder=1),
    "PRE_ADJUST": dict(color="#74AAEE", s=50,  alpha=0.80, zorder=2),
    "SPIKE":      dict(color="#CC44CC", s=50,  alpha=0.90, zorder=4),
    "OUTLIER":    dict(color="#FF8800", s=50,  alpha=0.90, zorder=4),
    # {subentity}: dict(color="#1155CC", s=50,  alpha=0.90, zorder=3) — added per flag
}


# ── Helper: detect unconfirmed B_TOOL steps for a subentity ───────────────────

def _detect_spikes(sub_df: pd.DataFrame) -> set:
    """
    Return a set of DATA_COLLECTION_TIME values that are unconfirmed B_TOOL steps:
    |B_TOOL[i] - B_TOOL[i-1]| >= B_TOOL_STEP_THRESHOLD  AND  the step is NOT
    sustained for MIN_PERSIST_RUNS consecutive rows (mirror of engine Phase 3,
    returning the positions that failed the confirmation check).
    """
    apc = (
        sub_df[sub_df["APC_B_TOOL"].notna()]
        .sort_values("DATA_COLLECTION_TIME")
        .reset_index(drop=True)
    )
    if len(apc) < 2:
        return set()

    bt = apc["APC_B_TOOL"].to_numpy()
    ts = apc["DATA_COLLECTION_TIME"].tolist()
    n  = len(bt)

    # First pass: identify confirmed step positions (same logic as engine)
    confirmed_pos = set()
    for i in range(1, n):
        if abs(bt[i] - bt[i - 1]) >= B_TOOL_STEP_THRESHOLD:
            end = i + 1 + MIN_PERSIST_RUNS
            if end <= n and all(
                abs(bt[j] - bt[i]) < B_TOOL_STEP_THRESHOLD
                for j in range(i + 1, end)
            ):
                confirmed_pos.add(i)

    # Collect step positions that were NOT confirmed → spikes
    spike_times = set()
    for i in range(1, n):
        if (abs(bt[i] - bt[i - 1]) >= B_TOOL_STEP_THRESHOLD
                and i not in confirmed_pos):
            spike_times.add(ts[i])

    return spike_times


# ── Helper: read control limits (matches JSL logic exactly) ───────────────────

def _get_limits(df: pd.DataFrame) -> dict:
    """
    For each WEC_LAYER, take STATISTICS_MEAN_DTT_UCL / _CENTERLINE / _LCL from
    the first IS_POR='True' row (chronological), matching the JSL script.
    Returns dict keyed by WEC_LAYER with sub-keys 'ucl', 'cl', 'lcl'.
    """
    limits: dict = {}
    por = df[df["IS_POR"].astype(str).str.lower() == "true"]
    for layer, grp in por.groupby("WEC_LAYER"):
        row = grp.sort_values("DATA_COLLECTION_TIME").iloc[0]
        limits[layer] = {
            "ucl": pd.to_numeric(
                row.get("STATISTICS_MEAN_DTT_UCL",        np.nan), errors="coerce"),
            "cl":  pd.to_numeric(
                row.get("STATISTICS_MEAN_DTT_CENTERLINE", np.nan), errors="coerce"),
            "lcl": pd.to_numeric(
                row.get("STATISTICS_MEAN_DTT_LCL",        np.nan), errors="coerce"),
        }
    return limits


# ── Helper: draw UCL/CL/LCL lines on an axis ──────────────────────────────────

def _add_limits(ax, ucl: float, cl: float, lcl: float) -> None:
    for val, tag, col in [
        (ucl, "UCL", "red"),
        (cl,  "",    "black"),   # solid CL; no label on the zero line
        (lcl, "LCL", "red"),
    ]:
        if pd.notna(val):
            ax.axhline(val, color=col, lw=0.9, ls="-", alpha=0.75)
            if tag:
                ax.text(
                    1.01, val, tag,
                    transform=ax.get_yaxis_transform(),
                    va="center", fontsize=12, color=col,
                )


# ── Helper: smoothing spline (mimics JMP Fit Spline lambda=0.1 Standardized) ────

def _add_spline(
    ax,
    x_series: pd.Series,
    y_series: pd.Series,
    color: str,
    lw: float = 0.9,
    frac: float = 0.30,
) -> None:
    """
    Smooth trend line using LOWESS (locally-weighted regression).
    LOWESS averages over a neighbourhood of `frac` of the data at each
    point so it responds to long-range trends rather than individual spikes —
    closely matching JMP's 'Fit Spline(0.1, Standardized)' character.

    Tuning levers:
      frac=0.20  tighter; tracks medium-scale trends
      frac=0.30  default; smooth long-range trend (JMP-like)
      frac=0.45  very smooth; near-global trend line

    Falls back to a high-smoothing cubic spline (s = n×3) when
    statsmodels is not installed.
    """
    xy = (
        pd.DataFrame({"x": x_series, "y": y_series})
        .dropna()
        .sort_values("x")
        .groupby("x", sort=True)["y"].mean()
        .reset_index()
    )
    if len(xy) < 5:
        return
    x_num = mdates.date2num(xy["x"].to_numpy())
    y_num = xy["y"].to_numpy()
    try:
        if _LOWESS_AVAILABLE:
            result = _sm_lowess(y_num, x_num, frac=frac, it=2, return_sorted=True)
            if len(result) >= 3:
                x_fine = np.linspace(result[0, 0], result[-1, 0],
                                     max(200, len(result) * 10))
                _cs = CubicSpline(result[:, 0], result[:, 1])
                ax.plot(
                    mdates.num2date(x_fine), _cs(x_fine),
                    color=color, lw=lw, alpha=0.75, zorder=6,
                )
            else:
                ax.plot(
                    mdates.num2date(result[:, 0]), result[:, 1],
                    color=color, lw=lw, alpha=0.75, zorder=6,
                )
        else:
            # Fallback: high-smoothing standardised cubic spline
            x_mn, x_sd = x_num.mean(), x_num.std() + 1e-12
            y_mn, y_sd = y_num.mean(), y_num.std() + 1e-12
            x_s  = (x_num - x_mn) / x_sd
            y_s  = (y_num - y_mn) / y_sd
            spl  = UnivariateSpline(x_s, y_s, s=len(x_s) * 3.0, k=3, ext=3)
            x_fit = np.linspace(x_s.min(), x_s.max(), 400)
            y_fit = spl(x_fit) * y_sd + y_mn
            ax.plot(
                mdates.num2date(x_fit * x_sd + x_mn), y_fit,
                color=color, lw=lw, alpha=0.75, zorder=6,
            )
    except Exception:
        pass


# ── Helper: variability panel (box + jittered strip) ──────────────────────────

def _variability_panel(
    ax,
    df: pd.DataFrame,
    y_col: str,
    grp_col: str,
    order: list,
    styles: dict,
    subentity: str,
    ucl: float,
    cl: float,
    lcl: float,
    label_col: str | None = None,
) -> None:
    """
    Boxplot + jittered strip for each group in `order`.
    `grp_col`   – column used for grouping (x-axis categories).
    `label_col` – if set, used to look up colour from `styles`
                  (for composite group keys like "LABEL\nPRODUCT").
    """
    present = [g for g in order if g in df[grp_col].values]
    if not present:
        return

    arrays = [
        df[df[grp_col] == g][y_col].dropna().to_numpy()
        for g in present
    ]
    arrays = [a if len(a) else np.array([np.nan]) for a in arrays]

    ax.boxplot(
        arrays,
        positions=list(range(len(present))),
        widths=0.45,
        patch_artist=True,
        boxprops=dict(facecolor="none", color="black", linewidth=0.8, zorder=5),
        medianprops=dict(color="black", linewidth=0.8, zorder=5),
        whiskerprops=dict(color="black", linewidth=0.8, zorder=5),
        capprops=dict(color="black", linewidth=0.8, zorder=5),
        flierprops=dict(marker=""),
        showfliers=False,
    )

    rng = np.random.default_rng(0)
    for i, g in enumerate(present):
        sub  = df[df[grp_col] == g]
        vals = sub[y_col].dropna()
        if vals.empty:
            continue
        # Determine colour from label column if composite key, else from grp_col
        if label_col:
            lbl = sub[label_col].mode().iloc[0] if not sub[label_col].mode().empty else g
        else:
            lbl = g
        st    = styles.get(lbl, styles.get(subentity, {}))
        color = st.get("color", "#AAAAAA")
        alpha = st.get("alpha", 0.7)
        jitter = rng.uniform(-0.18, 0.18, size=len(vals))
        ax.scatter(
            np.full(len(vals), i) + jitter,
            vals.to_numpy(),
            c=color, s=18, alpha=alpha, zorder=3, linewidths=0,
        )

    _add_limits(ax, ucl, cl, lcl)

    ax.set_xticks(range(len(present)))
    ax.set_xticklabels(present, fontsize=12, rotation=35, ha="right")
    ax.yaxis.grid(True, alpha=0.3)


# ── Helper: combined variability — outer=PROD_MOP_PILOT, inner=LABEL ─────────

def _combined_variability_panel(
    ax,
    df: pd.DataFrame,
    y_col: str,
    prod_col: str,
    label_col: str,
    prod_order: list,
    primary_lbl_order: list,
    styles: dict,
    subentity: str,
    ucl: float,
    cl: float,
    lcl: float,
) -> None:
    """
    Left section  — primary label groups (all data, no product split).
    Right section — PROD_MOP_PILOT × LABEL with inner order = primary_lbl_order
                    so the clean chamber group sits directly left of FLEET.
    A dotted separator divides the two sections.  Connect-means lines link
    group means within each product (right section only).
    """
    df = df.copy()
    df["_gc"] = df[prod_col].astype(str) + " " + df[label_col].astype(str)

    # ── Section 1: primary label groups ──────────────────────────────────────
    prim_present = [l for l in primary_lbl_order if l in df[label_col].values]

    # ── Section 2: product × label groups ────────────────────────────────────
    product_groups: list[str] = []
    for prod in prod_order:
        for lbl in primary_lbl_order:
            key = f"{prod} {lbl}"
            if ((df[prod_col].astype(str) == str(prod)) &
                    (df[label_col] == lbl)).any():
                product_groups.append(key)

    if not prim_present and not product_groups:
        return

    GAP    = 1.5
    n_prim = len(prim_present)
    n_prod = len(product_groups)

    prim_pos = list(range(n_prim))
    prod_pos = [n_prim + GAP + i for i in range(n_prod)]
    all_pos  = prim_pos + prod_pos
    all_grps = prim_present + product_groups

    all_arrays = []
    for i, g in enumerate(all_grps):
        if i < n_prim:
            arr = df[df[label_col] == g][y_col].dropna().to_numpy()
        else:
            arr = df[df["_gc"] == g][y_col].dropna().to_numpy()
        all_arrays.append(arr if len(arr) else np.array([np.nan]))

    ax.boxplot(
        all_arrays,
        positions=all_pos,
        widths=0.45,
        patch_artist=True,
        boxprops=dict(facecolor="none", color="black", linewidth=0.8, zorder=5),
        medianprops=dict(color="black", linewidth=0.8, zorder=5),
        whiskerprops=dict(color="black", linewidth=0.8, zorder=5),
        capprops=dict(color="black", linewidth=0.8, zorder=5),
        flierprops=dict(marker=""),
        showfliers=False,
    )

    rng = np.random.default_rng(0)
    for i, (pos, g) in enumerate(zip(all_pos, all_grps)):
        if i < n_prim:
            sub = df[df[label_col] == g]
            lbl = g
        else:
            sub = df[df["_gc"] == g]
            lbl = g.rsplit(" ", 1)[1]
        vals = sub[y_col].dropna()
        if vals.empty:
            continue
        st     = styles.get(lbl, styles.get(subentity, {}))
        color  = st.get("color", "#AAAAAA")
        alpha  = st.get("alpha", 0.7)
        jitter = rng.uniform(-0.18, 0.18, size=len(vals))
        ax.scatter(
            np.full(len(vals), pos) + jitter, vals.to_numpy(),
            c=color, s=st.get("s", 18), alpha=alpha, zorder=3, linewidths=0,
        )

    # Connect-means within each product group (right section only)
    prod_pos_map = {g: p for g, p in zip(product_groups, prod_pos)}
    for prod in prod_order:
        xs_cm, ys_cm = [], []
        for lbl in primary_lbl_order:
            key = f"{prod} {lbl}"
            if key not in prod_pos_map:
                continue
            sub = df[df["_gc"] == key][y_col].dropna()
            if sub.empty:
                continue
            xs_cm.append(prod_pos_map[key])
            ys_cm.append(float(sub.mean()))
        if len(xs_cm) >= 2:
            ax.plot(
                xs_cm, ys_cm,
                color="#333333", lw=1.0, ls="--", alpha=0.65,
                zorder=7, marker="D", markersize=3,
            )

    # Solid separator between primary and product sections
    if n_prim > 0 and n_prod > 0:
        ax.axvline(n_prim - 0.5 + GAP / 2, color="#444444",
                   lw=1.2, ls="-", alpha=0.8, zorder=0)

    # Dotted separators between product groups (right section)
    _prev_prod_name = None
    for _g, _pos in zip(product_groups, prod_pos):
        _pname = _g.rsplit(" ", 1)[0]
        if _prev_prod_name is not None and _pname != _prev_prod_name:
            ax.axvline(_pos - 0.5, color="#AAAAAA", lw=1.0, ls=":", alpha=0.8, zorder=0)
        _prev_prod_name = _pname

    # Build display labels: fleet groups show full "prod FLEET", others show label only
    fleet_lbl = "FLEET"
    display_labels = [
        g if (i < n_prim or g.endswith(" " + fleet_lbl))
        else g.rsplit(" ", 1)[1]
        for i, g in enumerate(all_grps)
    ]

    _add_limits(ax, ucl, cl, lcl)
    ax.set_xticks(all_pos)
    ax.set_xticklabels(display_labels, fontsize=12, rotation=40, ha="right")
    if all_pos:
        ax.set_xlim(all_pos[0] - 0.7, all_pos[-1] + 0.7)
    ax.yaxis.grid(True, alpha=0.3)


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_flag_visualizations(
    df: pd.DataFrame,
    flags_df: pd.DataFrame,
    chamber_detail: pd.DataFrame,
    script_path: Path,
    *,
    _skip_dev_filter: bool = False,
    _adhoc_prefix: str = "",
) -> None:
    """
    Generate 4-panel diagnostic PNGs for CHAMBER_CENTERING flags.

    Parameters
    ----------
    df             : full engine DataFrame (post DYNWAFER_001 / STATISTICS filter)
    flags_df       : final ranked flags table from assemble_and_rank
    chamber_detail : in-window detail rows (MAD_EXCLUDED column present)
    script_path    : Path(__file__) from the engine — used to locate flag_images dir
    """
    flag_dir = script_path.parent / "flag_images"
    flag_dir.mkdir(exist_ok=True)

    ch_flags = flags_df[flags_df["FLAG_TYPE"] == "CHAMBER_CENTERING"].copy()
    if ch_flags.empty:
        log.info("  No CHAMBER_CENTERING flags — skipping visualizations")
        return

    if DEV_MODE and not _skip_dev_filter:
        ch_flags = ch_flags[
            (ch_flags["SUBENTITY"] == DEV_FLAG_SUBENTITY) &
            (ch_flags["WEC_LAYER"]  == DEV_FLAG_LAYER)
        ]
        if ch_flags.empty:
            log.warning(
                "  DEV_MODE: no CHAMBER_CENTERING flag found for "
                "%s / %s — skipping", DEV_FLAG_SUBENTITY, DEV_FLAG_LAYER
            )
            return
        log.info(
            "  DEV_MODE: visualizing %s / %s",
            DEV_FLAG_SUBENTITY, DEV_FLAG_LAYER,
        )

    # Control limits (first IS_POR=True row per layer, matches JSL)
    limits = _get_limits(df)

    log.info("  Generating %d flag visualization(s) → %s", len(ch_flags), flag_dir)

    for _, flag_row in ch_flags.iterrows():
        subentity    = flag_row["SUBENTITY"]
        wec_layer    = flag_row["WEC_LAYER"]
        window_start = flag_row["WINDOW_START_DATE"]
        prio         = int(flag_row["PRIO"])
        n_wafers     = int(flag_row["N_WAFERS"])
        delta_nm      = float(flag_row.get("DELTA(nm) NEEDED",
                                           flag_row.get("MEAN_DTT_BIAS", np.nan)))
        current_btool = pd.to_numeric(flag_row.get("Current B_Tool", np.nan), errors="coerce")
        target_btool  = (float(current_btool) - delta_nm
                         if pd.notna(current_btool) and pd.notna(delta_nm)
                         else np.nan)
        _btool_cur_str = f"{current_btool:.4f}" if pd.notna(current_btool) else "n/a"
        _btool_tar_str = f"{target_btool:.4f}"  if pd.notna(target_btool)  else "n/a"

        # Per-flag style map (inject this flag's subentity colour)
        styles = {
            **_BASE_STYLES,
            subentity: dict(color="#1155CC", s=50, alpha=0.90, zorder=3),
        }

        # ── Subset to this layer, APC_FB_SUC==1 (matches engine population) ──
        layer_df = (
            df[
                (df["WEC_LAYER"] == wec_layer) &
                df["APC_FB_SUC"].eq(1)
            ]
            .sort_values("DATA_COLLECTION_TIME")
            .copy()
        )

        sub_df   = layer_df[layer_df["SUBENTITY"] == subentity].copy()
        fleet_df = layer_df[layer_df["SUBENTITY"] != subentity].copy()
        fleet_df["MAD_EXCLUDED"] = False

        # Merge MAD_EXCLUDED from chamber_detail for the flagged subentity
        if not chamber_detail.empty:
            det = chamber_detail[
                (chamber_detail["SUBENTITY"] == subentity) &
                (chamber_detail["WEC_LAYER"]  == wec_layer)
            ][["DATA_COLLECTION_TIME", "MAD_EXCLUDED"]].copy()
            sub_df = sub_df.merge(det, on="DATA_COLLECTION_TIME", how="left")
            sub_df["MAD_EXCLUDED"] = (
                sub_df["MAD_EXCLUDED"].fillna(False).infer_objects(copy=False).astype(bool)
            )
        else:
            sub_df["MAD_EXCLUDED"] = False

        # Detect unconfirmed B_TOOL spikes within the flagged subentity
        spike_times = _detect_spikes(sub_df)

        # ── Assign labels ─────────────────────────────────────────────────────
        def _label(row, _ws=window_start, _st=spike_times, _sub=subentity):
            t = row["DATA_COLLECTION_TIME"]
            if pd.notna(_ws) and t < _ws:
                return "PRE_ADJUST"
            if t in _st:
                return "SPIKE"
            if row["MAD_EXCLUDED"]:
                return "OUTLIER"
            return _sub

        sub_df["LABEL"]   = sub_df.apply(_label, axis=1)
        fleet_df["LABEL"] = "FLEET"

        plot_df = (
            pd.concat([fleet_df, sub_df], ignore_index=True)
            .sort_values("DATA_COLLECTION_TIME")
            .reset_index(drop=True)
        )

        # Scatter/legend order: FLEET behind (first), clean chamber on top (last)
        lbl_order = [
            l for l in ["FLEET", "PRE_ADJUST", "SPIKE", "OUTLIER", subentity]
            if l in plot_df["LABEL"].values
        ]
        # Variability panel inner order: fleet first, then chamber, then others
        inner_var_order = [
            l for l in ["FLEET", subentity, "OUTLIER", "SPIKE", "PRE_ADJUST"]
            if l in plot_df["LABEL"].values
        ]

        lim = limits.get(wec_layer, {"ucl": np.nan, "cl": np.nan, "lcl": np.nan})
        ucl, cl, lcl = lim["ucl"], lim["cl"], lim["lcl"]

        # ── Pre-compute group counts for dynamic figure width ────────────────────
        prod_order_comb = sorted(
            plot_df["PROD_MOP_PILOT"].dropna().astype(str).unique()
        )
        n_primary_var = len(inner_var_order)
        n_product_var = sum(
            1
            for _p in prod_order_comb
            for _l in inner_var_order
            if (
                (plot_df["PROD_MOP_PILOT"].astype(str) == _p) &
                (plot_df["LABEL"] == _l)
            ).any()
        )
        _PX_PER_GROUP = 60
        _DPI          = 130
        var_width_in  = max(3.0, (n_primary_var + 1.5 + n_product_var) * _PX_PER_GROUP / _DPI)
        ts_width_each = 7.0
        total_width   = ts_width_each * 2 + var_width_in

        # ── Build figure ────────────────────────────────────────────────────────
        fig, axes = plt.subplots(
            1, 3,
            figsize=(total_width, 7.5),
            gridspec_kw={"width_ratios": [ts_width_each, var_width_in, ts_width_each]},
        )
        fig.suptitle(
            f"{subentity}  |  {wec_layer}  "
            f"(N={n_wafers},  \u0394DTT needed = {delta_nm:+.4f} nm,  "
            f"B_TOOL cur={_btool_cur_str} \u2192 tar={_btool_tar_str})",
            fontsize=18, fontweight="bold", x=0.01, ha="left",
        )

        def _ts_scatter(ax_, x_col, y_col, df_):
            """Scatter plot layer-wide, colouring each label group."""
            for lbl in lbl_order:
                g = df_[df_["LABEL"] == lbl]
                if g.empty:
                    continue
                st = styles.get(lbl, styles[subentity])
                ax_.scatter(
                    g[x_col], g[y_col],
                    c=st["color"], s=st["s"],
                    alpha=st["alpha"], marker="o",
                    zorder=st["zorder"], linewidths=0,
                    label=lbl,
                )

        def _fmt_time(ax_):
            ax_.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m/%d"))
            ax_.xaxis.set_major_locator(
                mdates.WeekdayLocator(byweekday=0, interval=2)
            )
            plt.setp(ax_.xaxis.get_majorticklabels(), rotation=40, ha="right", fontsize=12)
            ax_.xaxis.grid(True, alpha=0.3)
            ax_.yaxis.grid(True, alpha=0.3)

        # Panel 1: DTT time series
        ax = axes[0]
        _ts_scatter(ax, "DATA_COLLECTION_TIME", "STATISTICS_MEAN_DTT_VALUE", plot_df)
        _add_limits(ax, ucl, cl, lcl)
        if pd.notna(window_start):
            ax.axvline(
                window_start, color="green", lw=1.1, ls=":", alpha=0.8,
                label="window start",
            )
        # Splines disabled
        # _fleet   = plot_df[plot_df["LABEL"] == "FLEET"]
        # _chamber = plot_df[plot_df["LABEL"] == subentity]
        # _add_spline(ax, _fleet["DATA_COLLECTION_TIME"],
        #             _fleet["STATISTICS_MEAN_DTT_VALUE"], "black", lw=0.9)
        # _add_spline(ax, _chamber["DATA_COLLECTION_TIME"],
        #             _chamber["STATISTICS_MEAN_DTT_VALUE"],
        #             styles[subentity]["color"], lw=1.2)
        _fmt_time(ax)
        ax.set_title(f"STATISTICS MEAN DTT VALUE 1278 HCCD NEST {wec_layer}",
                     fontsize=16, loc="left")
        ax.set_ylabel("STATISTICS_MEAN_DTT_VALUE (nm)", fontsize=12)
        ax.legend(fontsize=12, markerscale=0.9, loc="upper left")

        # Panel 2: variability — primary labels (left) + Product×Label (right)
        ax = axes[1]
        _combined_variability_panel(
            ax, plot_df, "STATISTICS_MEAN_DTT_VALUE",
            "PROD_MOP_PILOT", "LABEL",
            prod_order_comb, inner_var_order,
            styles, subentity,
            ucl, cl, lcl,
        )
        ax.set_title(f"STATISTICS MEAN DTT VALUE 1278 HCCD NEST {wec_layer}",
                     fontsize=16, loc="left")
        ax.set_ylabel("STATISTICS_MEAN_DTT_VALUE (nm)", fontsize=12)

        # Panel 3: B_TOOL time series
        ax = axes[2]
        btool_df = plot_df[plot_df["APC_B_TOOL"].notna()]
        _ts_scatter(ax, "DATA_COLLECTION_TIME", "APC_B_TOOL", btool_df)
        if pd.notna(window_start):
            ax.axvline(
                window_start, color="green", lw=1.1, ls=":", alpha=0.8,
                label="window start",
            )
        # Splines disabled
        # _fleet_bt   = btool_df[btool_df["LABEL"] == "FLEET"]
        # _chamber_bt = btool_df[btool_df["LABEL"] == subentity]
        # _add_spline(ax, _fleet_bt["DATA_COLLECTION_TIME"],
        #             _fleet_bt["APC_B_TOOL"], "black", lw=0.9)
        # _add_spline(ax, _chamber_bt["DATA_COLLECTION_TIME"],
        #             _chamber_bt["APC_B_TOOL"],
        #             styles[subentity]["color"], lw=1.2)
        _fmt_time(ax)
        ax.set_title(f"APC B TOOL {wec_layer}", fontsize=16, loc="left")
        ax.set_ylabel("APC_B_TOOL", fontsize=12)
        ax.legend(fontsize=12, markerscale=0.9, loc="upper left")
        # Target B_TOOL reference line
        if pd.notna(target_btool):
            ax.axhline(target_btool, color="blue", lw=0.9, ls="-", alpha=0.85)
            ax.text(1.01, target_btool, "TAR",
                    transform=ax.get_yaxis_transform(),
                    va="center", fontsize=12, color="blue")
            ax.text(0.02, target_btool, f"{target_btool:.4f}",
                    transform=ax.get_yaxis_transform(),
                    va="bottom", fontsize=9, color="blue", alpha=0.85)

        # Y-axis limits: 1.1× control limits or extreme chamber point, whichever wider
        _sub_dtt = plot_df[
            plot_df["LABEL"].isin([subentity, "OUTLIER", "PRE_ADJUST", "SPIKE"])
        ]["STATISTICS_MEAN_DTT_VALUE"].dropna()
        _y_lo_candidates = []
        _y_hi_candidates = []
        if pd.notna(lcl): _y_lo_candidates.append(1.1 * lcl)
        if pd.notna(ucl): _y_hi_candidates.append(1.1 * ucl)
        if not _sub_dtt.empty:
            _y_lo_candidates.append(float(_sub_dtt.min()))
            _y_hi_candidates.append(float(_sub_dtt.max()))
        if _y_lo_candidates and _y_hi_candidates:
            _y_lo = min(_y_lo_candidates)
            _y_hi = max(_y_hi_candidates)
            _pad  = (_y_hi - _y_lo) * 0.04
            axes[0].set_ylim(_y_lo - _pad, _y_hi + _pad)
            axes[1].set_ylim(_y_lo - _pad, _y_hi + _pad)

        plt.tight_layout(rect=[0, 0, 1, 0.94])

        prefix   = _adhoc_prefix if _adhoc_prefix else ""
        sep      = "_" if prefix else ""
        fname    = f"{prefix}{sep}{subentity}_{wec_layer}.png"
        out_path = flag_dir / fname
        fig.savefig(out_path, dpi=130, bbox_inches="tight")
        plt.close(fig)
        log.info("    Saved: %s", fname)

    log.info("  Flag images written to: %s", flag_dir)


# ── Ad hoc entry point: run analysis + visualize for any chamber × layer ──────

def generate_adhoc_visualization(
    subentity: str,
    layer_short: str,
    input_csv_path: str = None,
    script_path: Path = None,
    lookback_days: int | None = None,
) -> "dict | None":
    """
    Run the full centering analysis for one (SUBENTITY, LAYER_SHORT) pair
    and produce a diagnostic PNG without needing a pre-run flagging engine.

    Replicates the engine's B_TOOL window detection, MAD outlier tagging,
    and centering test so the image is identical to what the engine produces.

    Parameters
    ----------
    subentity      : e.g. "AME409_PM4"
    layer_short    : trailing part of WEC_LAYER, e.g. "M13" for "650_M13"
    input_csv_path : path to APC-enriched HCCD CSV; defaults to engine INPUT_CSV
    script_path    : Path to this file; used to resolve flag_images dir
    lookback_days  : if >0, keep only rows in the last N days (from max timestamp)

    Returns
    -------
    dict with keys SUBENTITY, Metal Layers, DELTA(nm) NEEDED, Current B_Tool, New BTOOL
    on success; None if any early-exit condition is hit.
    """
    import sys as _sys

    _script = script_path or Path(__file__)

    # Locate engine directory and put it on sys.path so we can import helpers
    _sdtt_dir = _script.parent
    if str(_sdtt_dir) not in _sys.path:
        _sys.path.insert(0, str(_sdtt_dir))

    try:
        from sDTT_flagging_engine import (
            INPUT_CSV              as _ENGINE_CSV,
            detect_btool_adjustments,
            _keep_mask_after_mad,
            _centering_test,
        )
    except ImportError as _e:
        log.error("Ad hoc mode requires sDTT_flagging_engine on the path: %s", _e)
        return

    _csv = Path(input_csv_path or _ENGINE_CSV)
    log.info("Ad hoc visualization: SUBENTITY=%s  LAYER_SHORT=%s", subentity, layer_short)
    log.info("  CSV: %s", _csv)

    # ── Load & basic prep (mirrors engine load block) ─────────────────────────
    df = pd.read_csv(_csv, low_memory=False)
    df["DATA_COLLECTION_TIME"]      = pd.to_datetime(
        df["DATA_COLLECTION_TIME"], format="mixed", dayfirst=False, errors="coerce"
    )
    df["APC_B_TOOL"]                = pd.to_numeric(df["APC_B_TOOL"], errors="coerce")
    df["STATISTICS_MEAN_DTT_VALUE"] = pd.to_numeric(
        df["STATISTICS_MEAN_DTT_VALUE"], errors="coerce"
    )
    df["IS_BSL"] = df["APC_PRODGROUP"].astype(str).str.startswith("BSL")
    df = df[df["STATISTICS_MEAN_DTT_VALUE"].notna()].reset_index(drop=True)

    if lookback_days is not None:
        try:
            _lookback = int(lookback_days)
        except (TypeError, ValueError):
            log.warning("Ad hoc: invalid lookback_days=%r; skipping lookback filter", lookback_days)
            _lookback = None
        if _lookback is not None and _lookback > 0:
            _rows_before = len(df)
            _max_ts = df["DATA_COLLECTION_TIME"].max()
            if pd.notna(_max_ts):
                _cutoff = _max_ts - pd.Timedelta(days=_lookback)
                df = df[df["DATA_COLLECTION_TIME"] >= _cutoff].reset_index(drop=True)
                log.info(
                    "  Lookback filter: last %d day(s), cutoff=%s (%d -> %d rows)",
                    _lookback, str(_cutoff)[:19], _rows_before, len(df)
                )
            else:
                log.warning("Ad hoc: DATA_COLLECTION_TIME has no valid timestamps; skipping lookback filter")
        elif _lookback is not None and _lookback <= 0:
            log.info("  Lookback filter disabled (lookback_days=%d)", _lookback)

    # ── Resolve WEC_LAYER from LAYER_SHORT ────────────────────────────────────
    matching = (
        df[df["WEC_LAYER"].astype(str).str.upper().str.endswith(layer_short.upper())]
        ["WEC_LAYER"].dropna().unique()
    )
    if len(matching) == 0:
        log.error("Ad hoc: no WEC_LAYER ending in '%s' found in dataset", layer_short)
        return
    if len(matching) > 1:
        log.warning(
            "Ad hoc: multiple WEC_LAYERs match '%s': %s — using: %s",
            layer_short, matching.tolist(), matching[0],
        )
    wec_layer = str(matching[0])

    if not (df["SUBENTITY"] == subentity).any():
        log.error("Ad hoc: SUBENTITY '%s' not found in dataset", subentity)
        return

    # ── Detect B_TOOL adjustment window (Phase 3 equivalent) ─────────────────
    df = detect_btool_adjustments(df)

    # ── Compute MAD outliers for this chamber × layer ─────────────────────────
    sub_rows = (
        df[
            (df["SUBENTITY"] == subentity) &
            (df["WEC_LAYER"]  == wec_layer) &
            df["APC_FB_SUC"].eq(1)
        ].copy()
    )
    if sub_rows.empty:
        log.error("Ad hoc: no APC_FB_SUC=1 rows for %s / %s", subentity, wec_layer)
        return

    window_start = sub_rows["WINDOW_START_DATE"].iloc[0]
    last_adj     = sub_rows["LAST_BTOOL_ADJ_DATE"].iloc[0]
    in_window    = (
        sub_rows[sub_rows["DATA_COLLECTION_TIME"] >= window_start]
        if pd.notna(window_start) else sub_rows
    )

    keep   = _keep_mask_after_mad(in_window, "STATISTICS_MEAN_DTT_VALUE")
    n_excl = int((~keep).sum())
    clean  = in_window[keep]
    n, mean, ci_lo, ci_hi = _centering_test(clean["STATISTICS_MEAN_DTT_VALUE"])

    # ── Build chamber_detail (MAD tags for visualizer) ────────────────────────
    chamber_detail = in_window.copy()
    chamber_detail["MAD_EXCLUDED"] = ~keep

    # ── Most recent APC_B_TOOL in assessment window ───────────────────────────
    _bs = in_window.sort_values("DATA_COLLECTION_TIME")["APC_B_TOOL"].dropna()
    current_btool = float(_bs.iloc[-1]) if len(_bs) > 0 else np.nan

    # ── Most recent Metal Layer label in assessment window ────────────────────
    _layer_label = (
        in_window.sort_values("DATA_COLLECTION_TIME")["LAYER"].dropna().iloc[-1]
        if "LAYER" in in_window.columns and in_window["LAYER"].notna().any()
        else ""
    )

    # ── Build synthetic flag row ──────────────────────────────────────────────
    mean_safe  = round(float(mean),  4) if pd.notna(mean)  else 0.0
    ci_lo_safe = round(float(ci_lo), 4) if pd.notna(ci_lo) else np.nan
    ci_hi_safe = round(float(ci_hi), 4) if pd.notna(ci_hi) else np.nan
    delta_nm_needed = round(-mean_safe, 4)
    new_btool = (
        round(current_btool - delta_nm_needed, 4)
        if pd.notna(current_btool) and pd.notna(delta_nm_needed)
        else np.nan
    )

    flags_df = pd.DataFrame([{
        "FLAG_TYPE":           "CHAMBER_CENTERING",
        "LAYER_SHORT":         layer_short,
        "WEC_LAYER":           wec_layer,
        "SUBENTITY":           subentity,
        "Metal Layers":        _layer_label,
        "DELTA(nm) NEEDED":    delta_nm_needed,
        "Current B_Tool":      current_btool,
        "New BTOOL":           new_btool,
        "N_WAFERS":            n,
        "MEAN_DTT_BIAS":       mean_safe,
        "CI_LOWER":            ci_lo_safe,
        "CI_UPPER":            ci_hi_safe,
        "LAST_BTOOL_ADJ_DATE": last_adj,
        "WINDOW_START_DATE":   window_start,
        "WINDOW_END_DATE":     sub_rows["DATA_COLLECTION_TIME"].max(),
        "N_OUTLIERS_EXCLUDED": n_excl,
        "PRIO":                0,
    }])

    log.info(
        "  Analysis result — N=%d  bias=%.4f nm  outliers=%d  "
        "window_start=%s  current_btool=%s",
        n, mean_safe, n_excl,
        str(window_start)[:10] if pd.notna(window_start) else "full range",
        f"{current_btool:.4f}" if pd.notna(current_btool) else "n/a",
    )

    generate_flag_visualizations(
        df, flags_df, chamber_detail, _script,
        _skip_dev_filter=True, _adhoc_prefix="ADHOC",
    )

    return {
        "SUBENTITY":        subentity,
        "Metal Layers":     _layer_label,
        "DELTA(nm) NEEDED": delta_nm_needed,
        "Current B_Tool":   current_btool,
        "New BTOOL":        new_btool,
    }


# ── Standalone execution — populate ADHOC_COMBINATIONS above and run ───────────

if __name__ == "__main__":
    import datetime as _dt
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    _records = []
    for _subentity, _layer_short in ADHOC_COMBINATIONS:
        _result = generate_adhoc_visualization(
            subentity=_subentity,
            layer_short=_layer_short,
            script_path=Path(__file__),
            lookback_days=ADHOC_LOOKBACK_DAYS,
        )
        if _result is not None:
            _records.append(_result)
        else:
            log.warning("Ad hoc: skipping %s / %s (analysis returned None)", _subentity, _layer_short)

    if _records:
        _out_dir = Path(__file__).parent / "integrated_output"
        _out_dir.mkdir(exist_ok=True)
        _today = _dt.date.today().strftime("%Y%m%d")
        _out_csv = _out_dir / f"sDTT_adhoc_flags_{_today}.csv"
        pd.DataFrame(
            _records,
            columns=["SUBENTITY", "Metal Layers", "DELTA(nm) NEEDED", "Current B_Tool", "New BTOOL"],
        ).to_csv(_out_csv, index=False)
        log.info("Ad hoc CSV written: %s  (%d row(s))", _out_csv, len(_records))
    else:
        log.warning("Ad hoc: no results to write — CSV not created")
