from __future__ import annotations

from collections.abc import Generator
import uuid

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _sqlite_columns(connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})")).all()}


def _sqlite_add_columns(connection, table: str, definitions: dict[str, str]) -> None:
    existing = _sqlite_columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def _migrate_sqlite(connection) -> None:
    _sqlite_add_columns(
        connection,
        "regions",
        {
            "province_code": "VARCHAR(12) DEFAULT ''",
            "city_code": "VARCHAR(12) DEFAULT ''",
            "district_code": "VARCHAR(12) DEFAULT ''",
            "prefecture_city": "VARCHAR(60) DEFAULT ''",
            "macro_region": "VARCHAR(20) DEFAULT '未知'",
            "city_tier": "VARCHAR(30) DEFAULT '普通地级市'",
            "urban_rural": "VARCHAR(20) DEFAULT '未知'",
            "longitude": "FLOAT",
            "latitude": "FLOAT",
        },
    )
    _sqlite_add_columns(
        connection,
        "import_batches",
        {
            "source_platform_id": "INTEGER",
            "archived_path": "TEXT DEFAULT ''",
            "error_count": "INTEGER DEFAULT 0",
            "schema_report": "JSON DEFAULT '{}'",
        },
    )
    _sqlite_add_columns(
        connection,
        "appeals",
        {
            "source_platform_id": "INTEGER",
            "appeal_type_raw": "VARCHAR(120) DEFAULT ''",
            "quarter": "VARCHAR(8) DEFAULT ''",
            "content_hash": "VARCHAR(64) DEFAULT ''",
            "duplicate_group_key": "VARCHAR(64) DEFAULT ''",
            "canonical_appeal_id": "INTEGER",
            "is_canonical": "BOOLEAN DEFAULT 1",
        },
    )
    _sqlite_add_columns(
        connection,
        "appeal_annotations",
        {
            "taxonomy_version_id": "INTEGER",
            "auxiliary_labels": "JSON DEFAULT '[]'",
        },
    )
    _sqlite_add_columns(
        connection,
        "chat_sessions",
        {
            "public_id": "VARCHAR(36) DEFAULT ''",
            "user_id": "INTEGER",
            "context": "JSON DEFAULT '{}'",
            "expires_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    )
    _sqlite_add_columns(
        connection,
        "chat_messages",
        {
            "evidence": "JSON DEFAULT '[]'",
        },
    )
    _sqlite_add_columns(
        connection,
        "analysis_jobs",
        {
            "public_id": "VARCHAR(36) DEFAULT ''",
            "progress": "INTEGER DEFAULT 0",
            "payload": "JSON DEFAULT '{}'",
            "result": "JSON DEFAULT '{}'",
            "started_at": "DATETIME",
        },
    )
    if "embedding_candidate_count" not in _sqlite_columns(connection, "retrieval_logs"):
        connection.execute(
            text(
                "ALTER TABLE retrieval_logs "
                "ADD COLUMN embedding_candidate_count INTEGER DEFAULT 0"
            )
        )

    # Populate values that cannot be expressed as portable ALTER defaults.
    connection.execute(
        text(
            "UPDATE appeals SET appeal_type_raw = appeal_type "
            "WHERE appeal_type_raw IS NULL OR appeal_type_raw = ''"
        )
    )
    connection.execute(
        text(
            "UPDATE appeals SET quarter = "
            "substr(received_at, 1, 4) || '-Q' || "
            "CAST(((CAST(substr(received_at, 6, 2) AS INTEGER) - 1) / 3 + 1) AS INTEGER) "
            "WHERE quarter IS NULL OR quarter = ''"
        )
    )
    for table in ("chat_sessions", "analysis_jobs"):
        rows = connection.execute(
            text(f"SELECT id FROM {table} WHERE public_id IS NULL OR public_id = ''")
        ).all()
        for (row_id,) in rows:
            connection.execute(
                text(f"UPDATE {table} SET public_id = :public_id WHERE id = :id"),
                {"public_id": str(uuid.uuid4()), "id": row_id},
            )

    indexes = (
        "CREATE INDEX IF NOT EXISTS ix_regions_city_code ON regions(city_code)",
        "CREATE INDEX IF NOT EXISTS ix_appeals_quarter ON appeals(quarter)",
        "CREATE INDEX IF NOT EXISTS ix_appeals_content_hash ON appeals(content_hash)",
        "CREATE INDEX IF NOT EXISTS ix_appeals_canonical ON appeals(is_canonical)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_sessions_public_id ON chat_sessions(public_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_jobs_public_id ON analysis_jobs(public_id)",
    )
    for statement in indexes:
        connection.execute(text(statement))


def init_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            _migrate_sqlite(connection)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
