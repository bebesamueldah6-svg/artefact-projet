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

# Answer engine: "rules" (instant, no LLM) or "llm" (Ollama)
ENGINE = os.getenv("ENGINE", "rules")

# LLM (Ollama, local) - only used when ENGINE=llm
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "180"))

# SQL guardrails
SQL_MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "200"))
SQL_TIMEOUT_S = float(os.getenv("SQL_TIMEOUT_S", "5"))

# Observability
TRACE_DIR = ROOT / os.getenv("TRACE_DIR", "traces")
