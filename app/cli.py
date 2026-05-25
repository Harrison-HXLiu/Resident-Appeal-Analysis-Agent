from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal, init_database
from app.services.importer import import_excel


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="居民留言分析 Agent 数据工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="导入 Excel 数据")
    importer.add_argument("path", type=Path)
    importer.add_argument("--province", default=settings.default_province)
    importer.add_argument("--city", default=settings.default_city)
    importer.add_argument("--district", default="")
    args = parser.parse_args()

    init_database()
    if args.command == "import":
        with SessionLocal() as session:
            result = import_excel(session, args.path, args.province, args.city, args.district)
        if result.skipped:
            print(f"文件已导入过，批次 ID：{result.batch_id}")
        else:
            print(f"导入完成：总行数 {result.rows}，新增 {result.inserted}，更新 {result.updated}")


if __name__ == "__main__":
    main()
