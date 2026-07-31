from __future__ import annotations

from app.config import Settings, get_settings
from app.services.providers import OpenAICompatibleProvider, ProviderUnavailable


LLMUnavailable = ProviderUnavailable


class DeepSeekService(OpenAICompatibleProvider):
    """Backward-compatible facade for older service imports.

    New code should depend on ``ChatModelProvider``/``get_chat_provider``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        super().__init__(settings=settings)
