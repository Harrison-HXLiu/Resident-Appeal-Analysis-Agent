from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal, init_database
from app.services.embeddings import EmbeddingUnavailable, backfill_embeddings
from app.services.importer import import_excel
from app.services.rag import backfill_chunks, rebuild_fts_index


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="居民留言分析 Agent 数据工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="导入 Excel 数据")
    importer.add_argument("path", type=Path)
    importer.add_argument("--province", default=settings.default_province)
    importer.add_argument("--city", default=settings.default_city)
    importer.add_argument("--district", default="")
    subparsers.add_parser("rebuild-rag", help="回填 RAG chunk 并重建 SQLite FTS5 索引")
    embedding = subparsers.add_parser("build-embeddings", help="使用 DashScope 为 RAG chunk 构建语义向量索引")
    embedding.add_argument("--limit", type=int, default=None, help="仅处理前 N 条 chunk，用于小批量测试")
    args = parser.parse_args()

    init_database()
    if args.command == "import":
        with SessionLocal() as session:
            result = import_excel(session, args.path, args.province, args.city, args.district)
            chunk_count = backfill_chunks(session)
        rebuild_fts_index()
        if result.skipped:
            print(f"文件已导入过，批次 ID：{result.batch_id}，补齐 RAG chunk：{chunk_count}")
        else:
            print(
                f"导入完成：总行数 {result.rows}，新增 {result.inserted}，"
                f"更新 {result.updated}，补齐 RAG chunk：{chunk_count}"
            )
    elif args.command == "rebuild-rag":
        with SessionLocal() as session:
            chunk_count = backfill_chunks(session)
        rebuild_fts_index()
        print(f"RAG 索引已重建，新增 chunk：{chunk_count}")
    elif args.command == "build-embeddings":
        with SessionLocal() as session:
            try:
                count = backfill_embeddings(session, limit=args.limit)
            except EmbeddingUnavailable as exc:
                raise SystemExit(str(exc)) from exc
        print(f"Embedding 索引构建完成，新增/更新向量：{count}")


if __name__ == "__main__":
    main()
