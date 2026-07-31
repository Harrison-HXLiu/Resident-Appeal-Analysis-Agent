from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "居民政策诉求监测分析大模型"
    base_dir: Path = BASE_DIR
    database_url: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'instance' / 'appeals.db').as_posix()}"
    )
    uploads_dir: Path = BASE_DIR / "uploads"
    data_dir: Path = BASE_DIR / "data"
    archive_dir: Path = BASE_DIR / "instance" / "archive"
    snapshot_dir: Path = BASE_DIR / "instance" / "snapshots"
    search_index_dir: Path = BASE_DIR / "instance" / "search-index"
    export_dir: Path = BASE_DIR / "instance" / "exports"
    # Generic OpenAI-compatible provider.  The legacy DeepSeek variables below
    # remain supported so existing deployments keep working.
    model_api_key: str = os.getenv("MODEL_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
    model_base_url: str = os.getenv(
        "MODEL_BASE_URL", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    chat_model: str = os.getenv(
        "CHAT_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    )
    model_provider: str = os.getenv("MODEL_PROVIDER", "openai-compatible")
    model_timeout_seconds: float = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    model_max_retries: int = int(os.getenv("MODEL_MAX_RETRIES", "2"))
    model_input_cost_per_million: float = float(
        os.getenv("MODEL_INPUT_COST_PER_MILLION", "0")
    )
    model_output_cost_per_million: float = float(
        os.getenv("MODEL_OUTPUT_COST_PER_MILLION", "0")
    )
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    default_province: str = os.getenv("DEFAULT_PROVINCE", "江苏省")
    default_city: str = os.getenv("DEFAULT_CITY", "苏州市")
    auto_import_sample: bool = _as_bool(os.getenv("AUTO_IMPORT_SAMPLE"), True)
    taxonomy_status: str = os.getenv("TAXONOMY_STATUS", "trial")
    auth_required: bool = _as_bool(os.getenv("AUTH_REQUIRED"), False)
    bootstrap_admin_username: str = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "")
    bootstrap_admin_password: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    secure_cookies: bool = _as_bool(os.getenv("SECURE_COOKIES"), False)
    session_timeout_minutes: int = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    worker_enabled: bool = _as_bool(os.getenv("WORKER_ENABLED"), True)
    pregenerate_standard_reports: bool = _as_bool(
        os.getenv("PREGENERATE_STANDARD_REPORTS"), True
    )
    external_search_enabled: bool = _as_bool(os.getenv("EXTERNAL_SEARCH_ENABLED"), False)
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8000"))

    def prepare_directories(self) -> None:
        (self.base_dir / "instance").mkdir(parents=True, exist_ok=True)
        for directory in (
            self.uploads_dir,
            self.archive_dir,
            self.snapshot_dir,
            self.search_index_dir,
            self.export_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare_directories()
    return settings
