"""
Build APC mismatch cohorts and emit MWE query templates.

This script is the first implementation step for deterministic APC diagnostics:
- Finds mismatch examples from APC-enriched 60-day CSVs
- Captures five examples per APC model/area bucket
- Emits cohort CSVs and SQL template files for DB-side validation

Usage:
  c:/users/tbatson/My Programs/SQLPathFinder3/Python3/python.exe scripts/apc_mwe_diagnostics.py
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import pandas as pd


NULL_STRS = {"", "NAN", "NONE", "NULL", "[NULL]"}
MODEL_BUCKET_ORDER = [
    "MFGAMECT_FLOW_TEMP",
    "AMECT_ICCR2",
    "8AMEUBE",
    "8AMEUBE_GAS",
    "NO_AREA",
]
ACTIVE_BUCKET_ORDER = ["MFGAMECT_FLOW_TEMP", "AMECT_ICCR2", "NO_AREA"]
PM_TOKEN_RE = re.compile(r"PM\d+", re.IGNORECASE)


def target_bucket_order(input_name: str, include_legacy_models: bool) -> list[str]:
    if include_legacy_models:
        return MODEL_BUCKET_ORDER
    name = input_name.upper()
    if "_F32_" in name:
        return ["MFGAMECT_FLOW_TEMP", "NO_AREA"]
    return ACTIVE_BUCKET_ORDER


def normalize_text_col(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.upper()


def null_or_blank(series: pd.Series) -> pd.Series:
    text = normalize_text_col(series)
    return series.isna() | text.isin(NULL_STRS)


def extract_pm_token(series: pd.Series) -> pd.Series:
    text = normalize_text_col(series)
    token = text.str.extract(r"(PM\d+)", expand=False)
    return token


def model_bucket(df: pd.DataFrame) -> pd.Series:
    if "APC_AREA" not in df.columns:
        return pd.Series(["NO_AREA"] * len(df), index=df.index)
    area = normalize_text_col(df["APC_AREA"])
    return area.where(~area.isin(NULL_STRS), "NO_AREA")


def mismatch_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)

    out["model_bucket"] = model_bucket(df)

    sub = df["SUBENTITY"] if "SUBENTITY" in df.columns else pd.Series([pd.NA] * len(df))
    apc_sub = df["APC_SUBENTITY"] if "APC_SUBENTITY" in df.columns else pd.Series([pd.NA] * len(df))
    btool = df["APC_B_TOOL"] if "APC_B_TOOL" in df.columns else pd.Series([pd.NA] * len(df))
    area = df["APC_AREA"] if "APC_AREA" in df.columns else pd.Series([pd.NA] * len(df))

    sub_null = null_or_blank(sub)
    apc_sub_null = null_or_blank(apc_sub)
    btool_null = null_or_blank(btool)
    area_null = null_or_blank(area)

    sub_pm = extract_pm_token(sub)
    apc_sub_pm = extract_pm_token(apc_sub)

    both_sub_pop = (~sub_null) & (~apc_sub_null)
    both_pm_pop = both_sub_pop & sub_pm.notna() & apc_sub_pm.notna()

    format_only_subentity = both_pm_pop & (sub_pm == apc_sub_pm)
    pm_disagreement = both_pm_pop & (sub_pm != apc_sub_pm)
    unparsed_subentity = both_sub_pop & (~both_pm_pop)

    out["sub_pm_token"] = sub_pm
    out["apc_sub_pm_token"] = apc_sub_pm
    out["reason_format_only_subentity"] = format_only_subentity
    out["reason_pm_disagreement"] = pm_disagreement
    out["reason_unparsed_subentity"] = unparsed_subentity
    out["reason_area_present_btool_missing"] = (~area_null) & btool_null
    out["reason_no_area_match"] = area_null

    out["is_mismatch"] = (
        out["reason_pm_disagreement"]
        | out["reason_unparsed_subentity"]
        | out["reason_area_present_btool_missing"]
        | out["reason_no_area_match"]
    )

    return out


def choose_reason(row: pd.Series) -> str:
    if row["reason_pm_disagreement"]:
        return "pm_disagreement"
    if row["reason_unparsed_subentity"]:
        return "unparsed_subentity"
    if row["reason_area_present_btool_missing"]:
        return "area_present_btool_missing"
    if row["reason_no_area_match"]:
        return "no_area_match"
    return "unknown"


def stable_pick(df: pd.DataFrame, n: int) -> pd.DataFrame:
    sort_cols = [c for c in ["DATA_COLLECTION_TIME", "WAFER_ID", "WEC_OPERATION", "SPC_LOT", "WID"] if c in df.columns]
    if sort_cols:
        temp = df.copy()
        if "DATA_COLLECTION_TIME" in temp.columns:
            temp["_ts"] = pd.to_datetime(temp["DATA_COLLECTION_TIME"], errors="coerce", format="mixed")
            temp = temp.sort_values(["_ts"] + [c for c in sort_cols if c != "DATA_COLLECTION_TIME"], ascending=False)
            temp = temp.drop(columns=["_ts"])
        else:
            temp = temp.sort_values(sort_cols, ascending=False)
        return temp.head(n)
    return df.head(n)


def emit_sql_templates(sql_dir: Path) -> None:
    sql_dir.mkdir(parents=True, exist_ok=True)

    query_a = """-- Query A: Base chain integrity for wafer-operation keys
-- Replace placeholders with values from mismatch cohort CSVs.
SELECT
    w.WAFER,
    h.OPERATION,
    h.LOTOPERKEY,
    j.APC_DATA_ID,
    j.APC_OBJECT_NAME,
    j.APC_OBJECT_TYPE,
    j.CHANGE_TYPE,
    j.APC_JOB_TXN_TIME
FROM F_LOT_FLOW h
JOIN F_WAFERSLOTHIST w
  ON w.EXPECTED_LOT = h.LOT
 AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
 AND w.HISTORY_DELETED_FLAG = 'N'
LEFT JOIN P_APC_RUNJOB_HIST j
  ON j.LOTOPERKEY = h.LOTOPERKEY
 AND j.APC_OBJECT_TYPE = 'LOT'
 AND j.APC_OBJECT_NAME LIKE :apc_system_prefix
WHERE h.EXEC_FLAG NOT IN ('X','R','N')
  AND w.WAFER IN (:wafer_list)
  AND h.OPERATION IN (:operation_list)
ORDER BY w.WAFER, h.OPERATION, j.APC_JOB_TXN_TIME DESC;
"""

    query_b = """-- Query B: Area-tier hit map per wafer-operation
-- Run once per area in: MFGAMECT_FLOW_TEMP, AMECT_ICCR2, 8AMEUBE, 8AMEUBE_GAS
SELECT
    w.WAFER,
    h.OPERATION,
    d.ATTRIBUTE_NAME,
    d.ATTRIBUTE_VALUE,
    j.APC_OBJECT_NAME,
    j.APC_JOB_TXN_TIME
FROM F_LOT_FLOW h
JOIN F_WAFERSLOTHIST w
  ON w.EXPECTED_LOT = h.LOT
 AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
 AND w.HISTORY_DELETED_FLAG = 'N'
JOIN P_APC_RUNJOB_HIST j
  ON j.LOTOPERKEY = h.LOTOPERKEY
JOIN P_APC_TXN_DATA d
  ON d.APC_DATA_ID = j.APC_DATA_ID
WHERE h.EXEC_FLAG NOT IN ('X','R','N')
  AND w.WAFER IN (:wafer_list)
  AND h.OPERATION IN (:operation_list)
  AND j.APC_OBJECT_TYPE = 'LOT'
  AND j.APC_OBJECT_NAME LIKE :apc_system_prefix
  AND d.ATTRIBUTE_NAME IN ('AREA','B_TOOL','SUBENTITY','OPENRUNS','SETTING_USED')
  AND EXISTS (
      SELECT 1
      FROM P_APC_TXN_DATA d2
      WHERE d2.APC_DATA_ID = j.APC_DATA_ID
        AND d2.ATTRIBUTE_NAME = 'AREA'
        AND d2.ATTRIBUTE_VALUE = :target_area
  )
ORDER BY w.WAFER, h.OPERATION, j.APC_JOB_TXN_TIME DESC;
"""

    query_c = """-- Query C: Strict-match readiness (AREA + B_TOOL required)
WITH area_btool AS (
    SELECT
        w.WAFER,
        h.OPERATION,
        j.APC_JOB_TXN_TIME,
        MAX(CASE WHEN d.ATTRIBUTE_NAME = 'AREA' THEN d.ATTRIBUTE_VALUE END) AS area_val,
        MAX(CASE WHEN d.ATTRIBUTE_NAME = 'B_TOOL' THEN d.ATTRIBUTE_VALUE END) AS btool_val
    FROM F_LOT_FLOW h
    JOIN F_WAFERSLOTHIST w
      ON w.EXPECTED_LOT = h.LOT
     AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
     AND w.HISTORY_DELETED_FLAG = 'N'
    JOIN P_APC_RUNJOB_HIST j
      ON j.LOTOPERKEY = h.LOTOPERKEY
    JOIN P_APC_TXN_DATA d
      ON d.APC_DATA_ID = j.APC_DATA_ID
    WHERE h.EXEC_FLAG NOT IN ('X','R','N')
      AND w.WAFER IN (:wafer_list)
      AND h.OPERATION IN (:strict_ops)
      AND j.APC_OBJECT_TYPE = 'LOT'
      AND j.APC_OBJECT_NAME LIKE :apc_system_prefix
      AND d.ATTRIBUTE_NAME IN ('AREA','B_TOOL')
    GROUP BY w.WAFER, h.OPERATION, j.APC_JOB_TXN_TIME
)
SELECT
    WAFER,
    OPERATION,
    APC_JOB_TXN_TIME,
    area_val,
    btool_val,
    CASE WHEN area_val IS NOT NULL AND btool_val IS NOT NULL THEN 1 ELSE 0 END AS strict_match_ready
FROM area_btool
ORDER BY WAFER, OPERATION, APC_JOB_TXN_TIME DESC;
"""

    query_d = """-- Query D: Dedup winner explain rows for one wafer-operation
-- Use this to pull all competing rows then score externally with production priority.
SELECT
    w.WAFER,
    h.OPERATION,
    j.APC_OBJECT_NAME,
    j.CHANGE_TYPE,
    j.APC_JOB_TXN_TIME,
    d.ATTRIBUTE_NAME,
    d.ATTRIBUTE_VALUE
FROM F_LOT_FLOW h
JOIN F_WAFERSLOTHIST w
  ON w.EXPECTED_LOT = h.LOT
 AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
 AND w.HISTORY_DELETED_FLAG = 'N'
JOIN P_APC_RUNJOB_HIST j
  ON j.LOTOPERKEY = h.LOTOPERKEY
JOIN P_APC_TXN_DATA d
  ON d.APC_DATA_ID = j.APC_DATA_ID
WHERE h.EXEC_FLAG NOT IN ('X','R','N')
  AND w.WAFER = :wafer_id
  AND h.OPERATION = :operation
  AND j.APC_OBJECT_TYPE = 'LOT'
  AND j.APC_OBJECT_NAME LIKE :apc_system_prefix
  AND d.ATTRIBUTE_NAME IN ('AREA','B_TOOL','SUBENTITY','OPERATION','SETTING_USED','OPENRUNS')
ORDER BY j.APC_JOB_TXN_TIME DESC;
"""

    query_e = """-- Query E: Payload-shape inspection for FLOW_TEMP and UBE
SELECT
    w.WAFER,
    h.OPERATION,
    j.APC_OBJECT_NAME,
    j.APC_JOB_TXN_TIME,
    MAX(CASE WHEN d.ATTRIBUTE_NAME='AREA' THEN d.ATTRIBUTE_VALUE END) AS area_val,
    MAX(CASE WHEN d.ATTRIBUTE_NAME='B_TOOL' THEN d.ATTRIBUTE_VALUE END) AS b_tool_raw,
    MAX(CASE WHEN d.ATTRIBUTE_NAME='M_ETCHRATE' THEN d.ATTRIBUTE_VALUE END) AS m_etchrate_raw,
    MAX(CASE WHEN d.ATTRIBUTE_NAME='SETTING_USED' THEN d.ATTRIBUTE_VALUE END) AS setting_used_raw
FROM F_LOT_FLOW h
JOIN F_WAFERSLOTHIST w
  ON w.EXPECTED_LOT = h.LOT
 AND h.OUT_DATE BETWEEN w.SORTER_ACTION_DATE AND w.NEXT_SORTER_ACTION_DATE
 AND w.HISTORY_DELETED_FLAG = 'N'
JOIN P_APC_RUNJOB_HIST j
  ON j.LOTOPERKEY = h.LOTOPERKEY
JOIN P_APC_TXN_DATA d
  ON d.APC_DATA_ID = j.APC_DATA_ID
WHERE h.EXEC_FLAG NOT IN ('X','R','N')
  AND w.WAFER IN (:wafer_list)
  AND h.OPERATION IN (:operation_list)
  AND j.APC_OBJECT_TYPE = 'LOT'
  AND j.APC_OBJECT_NAME LIKE :apc_system_prefix
  AND d.ATTRIBUTE_NAME IN ('AREA','B_TOOL','M_ETCHRATE','SETTING_USED')
GROUP BY w.WAFER, h.OPERATION, j.APC_OBJECT_NAME, j.APC_JOB_TXN_TIME
ORDER BY w.WAFER, h.OPERATION, j.APC_JOB_TXN_TIME DESC;
"""

    files = {
        "query_a_base_chain.sql": query_a,
        "query_b_area_tier_hit_map.sql": query_b,
        "query_c_strict_match_readiness.sql": query_c,
        "query_d_dedup_winner_explain.sql": query_d,
        "query_e_payload_shape_inspection.sql": query_e,
    }

    for name, content in files.items():
        (sql_dir / name).write_text(content, encoding="utf-8")


def build_for_file(
    input_csv: Path, out_root: Path, per_model: int, include_legacy_models: bool
) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(input_csv, low_memory=False)
    flags = mismatch_flags(df)
    merged = pd.concat([df, flags], axis=1)

    mismatch = merged[merged["is_mismatch"]].copy()

    bucket_order = target_bucket_order(input_csv.name, include_legacy_models)

    if not include_legacy_models:
        mismatch = mismatch[mismatch["model_bucket"].isin(ACTIVE_BUCKET_ORDER)].copy()

    class_summary = pd.DataFrame(
        {
            "class_name": [
                "format_only_subentity",
                "pm_disagreement",
                "unparsed_subentity",
                "area_present_btool_missing",
                "no_area_match",
            ],
            "rows": [
                int(flags["reason_format_only_subentity"].sum()),
                int(flags["reason_pm_disagreement"].sum()),
                int(flags["reason_unparsed_subentity"].sum()),
                int(flags["reason_area_present_btool_missing"].sum()),
                int(flags["reason_no_area_match"].sum()),
            ],
        }
    )

    mismatch["mismatch_reason"] = mismatch.apply(choose_reason, axis=1)

    cohort_rows = []
    for model in bucket_order:
        chunk = mismatch[mismatch["model_bucket"] == model].copy()
        if chunk.empty:
            continue
        picked = stable_pick(chunk, per_model)
        cohort_rows.append(picked)

    if not cohort_rows:
        return pd.DataFrame(), pd.DataFrame()

    cohort = pd.concat(cohort_rows, ignore_index=True)

    stem = input_csv.stem
    out_dir = out_root / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    cohort_path = out_dir / f"{stem}_mwe_mismatch_cohort.csv"
    cohort.to_csv(cohort_path, index=False)

    model_summary = (
        mismatch.groupby(["model_bucket", "mismatch_reason"], dropna=False)
        .size()
        .reset_index(name="rows")
        .sort_values(["model_bucket", "rows"], ascending=[True, False])
    )
    model_summary.to_csv(out_dir / f"{stem}_mwe_mismatch_summary.csv", index=False)
    class_summary.to_csv(out_dir / f"{stem}_mwe_class_summary.csv", index=False)

    bucket_counts = (
        cohort.groupby("model_bucket", dropna=False)
        .size()
        .reindex(bucket_order, fill_value=0)
        .reset_index(name="picked_examples")
        .rename(columns={"index": "model_bucket"})
    )
    bucket_counts["target_examples"] = per_model
    bucket_counts["target_met"] = bucket_counts["picked_examples"] >= bucket_counts["target_examples"]
    bucket_counts.to_csv(out_dir / f"{stem}_mwe_model_target_coverage.csv", index=False)

    key_cols = [
        c
        for c in [
            "WAFER_ID",
            "WEC_OPERATION",
            "SUBENTITY",
            "APC_SUBENTITY",
            "APC_AREA",
            "APC_B_TOOL",
            "DATA_COLLECTION_TIME",
            "model_bucket",
            "mismatch_reason",
        ]
        if c in cohort.columns
    ]
    cohort[key_cols].to_csv(out_dir / f"{stem}_mwe_query_keys.csv", index=False)

    if "APC_AREA" in df.columns:
        f32_drift = pd.DataFrame()
        if "_F32_" in input_csv.name.upper():
            area = normalize_text_col(df["APC_AREA"])
            non_null = ~null_or_blank(df["APC_AREA"])
            policy_drift = df[non_null & (area != "MFGAMECT_FLOW_TEMP")].copy()
            if not policy_drift.empty:
                keep = [c for c in ["WAFER_ID", "WEC_OPERATION", "SUBENTITY", "APC_AREA", "DATA_COLLECTION_TIME"] if c in policy_drift.columns]
                f32_drift = policy_drift[keep]
        if not f32_drift.empty:
            f32_drift.to_csv(out_dir / f"{stem}_f32_policy_drift.csv", index=False)

    pm_confusion = mismatch[
        mismatch["mismatch_reason"].isin(["pm_disagreement", "unparsed_subentity"])
    ].copy()
    if not pm_confusion.empty:
        by_cols = [c for c in ["model_bucket", "WEC_OPERATION", "sub_pm_token", "apc_sub_pm_token"] if c in pm_confusion.columns]
        conf = (
            pm_confusion.groupby(by_cols, dropna=False)
            .size()
            .reset_index(name="rows")
            .sort_values("rows", ascending=False)
        )
        conf.to_csv(out_dir / f"{stem}_pm_confusion_by_operation.csv", index=False)

    return model_summary.assign(source_file=input_csv.name), bucket_counts.assign(source_file=input_csv.name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build APC MWE mismatch cohorts and query templates.")
    parser.add_argument("--per-model", type=int, default=5, help="Mismatch samples to keep per APC model bucket.")
    parser.add_argument(
        "--inputs",
        nargs="*",
        default=[
            "integrated_output/1278sDTT_HCCD_D1V_60day_APC.csv",
            "integrated_output/1278sDTT_HCCD_F32_60day_APC.csv",
        ],
        help="Input APC CSV paths, relative to repo root.",
    )
    parser.add_argument(
        "--output-dir",
        default="debug/mwe_apc",
        help="Output root folder for cohorts/summaries/templates, relative to repo root.",
    )
    parser.add_argument(
        "--include-legacy-models",
        action="store_true",
        help="Include legacy model buckets (8AMEUBE, 8AMEUBE_GAS) in mismatch accounting.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    out_root = root / args.output_dir
    out_root.mkdir(parents=True, exist_ok=True)

    all_summaries = []
    all_coverages = []
    for rel in args.inputs:
        path = root / rel
        if not path.exists():
            print(f"SKIP missing input: {path}")
            continue
        print(f"Processing {path.name} ...")
        summary, coverage = build_for_file(path, out_root, args.per_model, args.include_legacy_models)
        if not summary.empty:
            all_summaries.append(summary)
        if not coverage.empty:
            all_coverages.append(coverage)

    sql_dir = out_root / "sql_templates"
    emit_sql_templates(sql_dir)

    if all_summaries:
        combined = pd.concat(all_summaries, ignore_index=True)
        combined.to_csv(out_root / "mwe_model_summary_all_sources.csv", index=False)

    if all_coverages:
        coverage_all = pd.concat(all_coverages, ignore_index=True)
        coverage_all.to_csv(out_root / "mwe_target_coverage_all_sources.csv", index=False)
        gaps = coverage_all[~coverage_all["target_met"]].copy()
        gaps.to_csv(out_root / "mwe_target_coverage_gaps.csv", index=False)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] APC MWE diagnostics generation complete.")
    print(f"Outputs: {out_root}")


if __name__ == "__main__":
    main()
