from __future__ import annotations

from collections.abc import Generator

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


def init_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            retrieval_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(retrieval_logs)")).all()
            }
            if "embedding_candidate_count" not in retrieval_columns:
                connection.execute(
                    text(
                        "ALTER TABLE retrieval_logs "
                        "ADD COLUMN embedding_candidate_count INTEGER DEFAULT 0"
                    )
                )


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
