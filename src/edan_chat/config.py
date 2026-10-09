"""Central configuration (paths, model names, limits). Overridable via environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

PDF_URL = os.getenv(
    "PDF_URL",
    "https://www.cei.ci/wp-content/uploads/2025/12/EDAN_2025_RESULTAT_NATIONAL_DETAILS.pdf",
)
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
PDF_PATH = RAW_DIR / "EDAN_2025_RESULTAT_NATIONAL_DETAILS.pdf"
DB_PATH = PROCESSED_DIR / "edan.duckdb"
MANIFEST_PATH = PROCESSED_DIR / "manifest.json"

# LLM provider (any OpenAI-compatible chat endpoint). "none" = deterministic paths only.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()  # groq | gemini | ollama | none
_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash",
               "GEMINI_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b", ""),
}
_base, _model, _key_env = _PROVIDERS.get(LLM_PROVIDER, ("", "", ""))
LLM_BASE_URL = os.getenv("LLM_BASE_URL", _base)
LLM_MODEL = os.getenv("LLM_MODEL", _model)
LLM_API_KEY = os.getenv("LLM_API_KEY") or (os.getenv(_key_env, "") if _key_env else "")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "60"))

# Speech-to-text for voice questions (Whisper, OpenAI-compatible; Groq by default)
STT_BASE_URL = os.getenv("STT_BASE_URL", "https://api.groq.com/openai/v1")
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
STT_API_KEY = os.getenv("STT_API_KEY") or os.getenv("GROQ_API_KEY", "")

# SQL guardrails
SQL_MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "300"))
SQL_TIMEOUT_S = float(os.getenv("SQL_TIMEOUT_S", "5"))

# Retrieval (RAG path)
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "8"))

# Observability & caching
TRACE_DIR = ROOT / os.getenv("TRACE_DIR", "traces")
CACHE_DIR = ROOT / os.getenv("CACHE_DIR", ".cache")
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") == "1"
