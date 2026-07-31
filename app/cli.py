from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal, init_database
from app.services.inventory import write_inventory
from app.services.importer import import_excel
from app.services.rag import backfill_chunks, rebuild_fts_index
from app.services.snapshots import build_quarter_snapshot


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="居民留言分析 Agent 数据工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="导入 Excel、CSV 或 Parquet 数据")
    importer.add_argument("path", type=Path)
    importer.add_argument("--province", default=settings.default_province)
    importer.add_argument("--city", default=settings.default_city)
    importer.add_argument("--district", default="")
    importer.add_argument("--platform-code", default="")
    importer.add_argument("--platform-name", default="")
    importer.add_argument("--city-code", default="")
    importer.add_argument("--district-code", default="")
    snapshot = subparsers.add_parser("snapshot", help="构建季度 Parquet、聚合与检索快照")
    snapshot.add_argument("quarter", help="例如 2025-Q4")
    inventory = subparsers.add_parser("inventory", help="盘点多来源 Excel 表头与规模")
    inventory.add_argument("root", type=Path)
    inventory.add_argument(
        "--output",
        type=Path,
        default=settings.base_dir / "instance" / "source-inventory.json",
    )
    subparsers.add_parser("rebuild-rag", help="回填 RAG chunk 并重建 SQLite FTS5 索引")
    args = parser.parse_args()

    init_database()
    if args.command == "import":
        with SessionLocal() as session:
            result = import_excel(
                session,
                args.path,
                args.province,
                args.city,
                args.district,
                source_platform_code=args.platform_code,
                source_platform_name=args.platform_name,
                city_code=args.city_code,
                district_code=args.district_code,
            )
            chunk_count = backfill_chunks(session)
        if settings.database_url.startswith("sqlite"):
            rebuild_fts_index()
        if result.skipped:
            print(f"文件已导入过，批次 ID：{result.batch_id}，补齐 RAG chunk：{chunk_count}")
        else:
            print(
                f"导入完成：总行数 {result.rows}，新增 {result.inserted}，"
                f"更新 {result.updated}，补齐 RAG chunk：{chunk_count}"
            )
    elif args.command == "snapshot":
        with SessionLocal() as session:
            snapshot_record = build_quarter_snapshot(session, args.quarter)
        print(
            f"季度快照完成：{snapshot_record.quarter} v{snapshot_record.version}，"
            f"原始 {snapshot_record.row_count}，去重 {snapshot_record.canonical_count}"
        )
    elif args.command == "inventory":
        result = write_inventory(args.root, args.output)
        print(
            f"盘点完成：{result['file_count']}个文件，约{result['estimated_rows']}行，"
            f"{result['unique_header_patterns']}种表头；结果写入 {args.output}"
        )
    elif args.command == "rebuild-rag":
        with SessionLocal() as session:
            chunk_count = backfill_chunks(session)
        rebuild_fts_index()
        print(f"RAG 索引已重建，新增 chunk：{chunk_count}")


if __name__ == "__main__":
    main()
