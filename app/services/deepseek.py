from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from app.config import Settings, get_settings


class LLMUnavailable(RuntimeError):
    pass


class DeepSeekService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = bool(self.settings.deepseek_api_key.strip())
        self._client = (
            OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url=self.settings.deepseek_base_url,
                timeout=60.0,
                max_retries=2,
            )
            if self.enabled
            else None
        )

    @property
    def model_name(self) -> str:
        return self.settings.deepseek_model

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if not self._client:
            raise LLMUnavailable("尚未配置 DEEPSEEK_API_KEY。")
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or ""

    def stream_complete(self, system_prompt: str, user_prompt: str):
        if not self._client:
            raise LLMUnavailable("尚未配置 DEEPSEEK_API_KEY。")
        stream = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self._client:
            raise LLMUnavailable("尚未配置 DEEPSEEK_API_KEY。")
        response = self._client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

