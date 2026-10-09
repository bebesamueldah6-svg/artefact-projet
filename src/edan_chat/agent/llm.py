"""OpenAI-compatible chat client (Groq, Gemini, Ollama, ...) with token usage and response cache."""

from __future__ import annotations

import json
import time

import httpx

from edan_chat import config
from edan_chat.agent.cache import DiskCache


class LLMError(RuntimeError):
    pass


class ChatModel:
    """chat(messages, json_mode) -> (text, usage). `usage` has prompt/completion token counts."""

    model = "none"

    def chat(self, messages: list[dict], json_mode: bool = False) -> tuple[str, dict]:
        raise NotImplementedError

    def available(self) -> bool:
        return False


class OpenAICompatChat(ChatModel):
    def __init__(self, base_url: str = config.LLM_BASE_URL, model: str = config.LLM_MODEL,
                 api_key: str = config.LLM_API_KEY, timeout_s: float = config.LLM_TIMEOUT_S,
                 provider: str = config.LLM_PROVIDER):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.timeout_s, self.provider = timeout_s, provider
        self.cache = DiskCache("llm")

    def available(self) -> bool:
        if self.provider == "none" or not self.base_url or not self.model:
            return False
        return bool(self.api_key) or self.provider == "ollama"

    def chat(self, messages: list[dict], json_mode: bool = False) -> tuple[str, dict]:
        if not self.available():
            raise LLMError("Aucun LLM configuré (voir .env.example) / no LLM configured.")
        key = self.cache.key(self.model, messages, json_mode)
        if (hit := self.cache.get(key)) is not None:
            return hit["text"], {**hit["usage"], "cached": True}

        payload = {"model": self.model, "messages": messages, "temperature": 0}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for attempt in range(3):  # free tiers rate-limit: back off on 429 / 5xx
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=payload, headers=headers,
                               timeout=self.timeout_s)
            except httpx.HTTPError as e:
                raise LLMError(f"LLM injoignable ({self.provider}) : {e}") from e
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            raise LLMError(f"LLM {self.provider} HTTP {r.status_code} : {r.text[:200]}")
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = {k: (data.get("usage") or {}).get(k, 0) for k in ("prompt_tokens", "completion_tokens")}
        self.cache.set(key, {"text": text, "usage": usage})
        return text, usage


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences and surrounding text."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LLMError(f"Réponse non JSON du modèle : {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON invalide du modèle : {e}") from e
