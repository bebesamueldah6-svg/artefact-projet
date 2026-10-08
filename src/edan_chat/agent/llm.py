"""Minimal Ollama chat client (local LLM, no data leaves the machine)."""

from __future__ import annotations

import json
from typing import Protocol

import httpx

from edan_chat import config


class LLMError(RuntimeError):
    pass


class ChatModel(Protocol):
    def chat(self, messages: list[dict], json_mode: bool = False) -> str: ...


class OllamaChat:
    def __init__(self, model: str = config.LLM_MODEL, host: str = config.OLLAMA_HOST,
                 timeout_s: float = config.LLM_TIMEOUT_S):
        self.model, self.host, self.timeout_s = model, host.rstrip("/"), timeout_s

    def chat(self, messages: list[dict], json_mode: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama injoignable ou en erreur ({self.host}, modèle {self.model}) : {e}") from e
        return r.json()["message"]["content"]

    def available(self) -> bool:
        try:
            tags = httpx.get(f"{self.host}/api/tags", timeout=3).json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return False
        names = {m["name"] for m in tags.get("models", [])}
        return self.model in names or f"{self.model}:latest" in names


def parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LLMError(f"Réponse non JSON du modèle : {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON invalide du modèle : {e}") from e
