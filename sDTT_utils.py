import numpy as np
import pandas as pd
from scipy import stats

# ── Statistical helpers shared between sDTT engines ────────────────────────────
#
# Parameters that were formerly module-level constants (CI_CONFIDENCE,
# MIN_FLAG_N, MAD_MULTIPLIER) are now explicit function parameters with defaults
# that match the original values.  Callers that rely on the defaults get
# identical behaviour; callers in the product engine can pass their own values.


def _mad(series: pd.Series) -> float:
    """Median absolute deviation (NaN-safe)."""
    clean = series.dropna()
    if len(clean) == 0:
        return np.nan
    return (clean - clean.median()).abs().median()


def _centering_test(values: pd.Series, ci_confidence: float = 0.95):
    """
    Compute N, mean, and 95% CI for a numeric series.

    Returns (n, mean, ci_lower, ci_upper).  Returns NaN for CI bounds when
    fewer than 2 non-null observations are present.
    """
    clean = values.dropna()
    n = len(clean)
    if n < 2:
        return n, float('nan'), float('nan'), float('nan')
    mean = clean.mean()
    se   = clean.std(ddof=1) / np.sqrt(n)
    ci   = stats.t.interval(ci_confidence, df=n - 1, loc=mean, scale=se)
    return n, mean, ci[0], ci[1]


def _is_flagged(n: int, ci_lower: float, ci_upper: float, min_flag_n: int = 5) -> bool:
    """Return True when the group has enough data and its CI excludes zero."""
    if n < min_flag_n:
        return False
    if any(np.isnan(v) for v in (ci_lower, ci_upper)):
        return False
    return not (ci_upper >= 0 and ci_lower <= 0)   # CI must not straddle zero


def _keep_mask_after_mad(
    group_df: pd.DataFrame, col: str, mad_multiplier: float = 3.0
) -> pd.Series:
    """
    Return a boolean Series (same index as group_df) that is True for rows to KEEP.
    Rows where |value − median| > mad_multiplier × MAD are treated as outliers.
    Null rows are always kept (they are excluded from the centering test naturally).
    """
    clean = group_df[col].dropna()
    if len(clean) < 3:
        return pd.Series(True, index=group_df.index)
    med = clean.median()
    mad = _mad(clean)
    if mad == 0:
        return pd.Series(True, index=group_df.index)
    keep = group_df[col].isna() | ((group_df[col] - med).abs() <= mad_multiplier * mad)
    return keep
