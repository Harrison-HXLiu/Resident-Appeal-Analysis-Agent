from __future__ import annotations

from pathlib import Path
from typing import Any


ALLOWED_DIMENSIONS = {
    "province",
    "prefecture_city",
    "district",
    "macro_region",
    "city_tier",
    "urban_rural",
    "topic_l1",
    "topic_l2",
    "appeal_type",
    "department",
}
ALLOWED_FILTERS = ALLOWED_DIMENSIONS | {"quarter", "city_code", "district_code"}


def _dataset_glob(dataset_path: str | Path) -> str:
    path = Path(dataset_path).resolve()
    if not path.is_dir():
        raise ValueError("Parquet快照目录不存在")
    parquet_files = list(path.rglob("*.parquet"))
    if not parquet_files:
        raise ValueError("Parquet快照不包含数据文件")
    return f"{path.as_posix()}/**/*.parquet"


def validate_snapshot_dataset(
    dataset_path: str | Path,
    *,
    expected_rows: int,
    expected_canonical_rows: int,
) -> dict[str, int]:
    """Use DuckDB to independently verify an immutable Parquet snapshot."""

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise RuntimeError("校验Parquet快照需要安装 duckdb") from exc

    parquet_glob = _dataset_glob(dataset_path)
    connection = duckdb.connect(database=":memory:")
    try:
        row = connection.execute(
            """
            SELECT
                count(*)::BIGINT AS row_count,
                sum(CASE WHEN is_canonical THEN 1 ELSE 0 END)::BIGINT AS canonical_count,
                count(DISTINCT quarter)::BIGINT AS quarter_count
            FROM read_parquet(?, hive_partitioning=false, union_by_name=true)
            """,
            [parquet_glob],
        ).fetchone()
    finally:
        connection.close()
    actual = {
        "row_count": int(row[0] or 0),
        "canonical_count": int(row[1] or 0),
        "quarter_count": int(row[2] or 0),
    }
    if actual["row_count"] != expected_rows:
        raise ValueError(
            f"Parquet行数校验失败：数据库{expected_rows}，快照{actual['row_count']}"
        )
    if actual["canonical_count"] != expected_canonical_rows:
        raise ValueError(
            "Parquet去重事件数校验失败："
            f"数据库{expected_canonical_rows}，快照{actual['canonical_count']}"
        )
    if actual["quarter_count"] != 1:
        raise ValueError("Parquet季度边界校验失败")
    return actual


def aggregate_snapshot(
    dataset_path: str | Path,
    *,
    dimension: str,
    filters: dict[str, str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Run a whitelisted ad-hoc aggregation without exposing arbitrary SQL."""

    if dimension not in ALLOWED_DIMENSIONS:
        raise ValueError(f"不允许的聚合维度：{dimension}")
    safe_filters = filters or {}
    invalid_filters = set(safe_filters) - ALLOWED_FILTERS
    if invalid_filters:
        raise ValueError(f"不允许的筛选字段：{sorted(invalid_filters)}")
    bounded_limit = max(1, min(int(limit), 1000))
    parquet_glob = _dataset_glob(dataset_path)
    clauses = ["is_canonical = true"]
    parameters: list[object] = [parquet_glob]
    for key, value in safe_filters.items():
        clauses.append(f'"{key}" = ?')
        parameters.append(value)
    parameters.append(bounded_limit)
    query = f"""
        SELECT
            coalesce(CAST("{dimension}" AS VARCHAR), '未知') AS name,
            count(*)::BIGINT AS event_count
        FROM read_parquet(?, hive_partitioning=false, union_by_name=true)
        WHERE {" AND ".join(clauses)}
        GROUP BY 1
        ORDER BY event_count DESC, name ASC
        LIMIT ?
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("查询Parquet快照需要安装 duckdb") from exc
    connection = duckdb.connect(database=":memory:")
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    return [{"name": str(name), "event_count": int(count)} for name, count in rows]
