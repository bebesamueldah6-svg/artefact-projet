"""Small disk cache for LLM responses, keyed by dataset version.

Invalidation rule: every entry lives under CACHE_DIR/<dataset_version>/, where the dataset
version is derived from the PDF hash (manifest.json). Re-ingesting a different PDF changes the
version, so stale answers are never reused; deleting CACHE_DIR clears everything.
SQL results are cached in memory only (DuckDB is fast; the process restarts on re-ingest).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from edan_chat import config


@lru_cache(maxsize=1)
def dataset_version() -> str:
    try:
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))["dataset_version"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return "unversioned"


class DiskCache:
    def __init__(self, namespace: str, enabled: bool = config.CACHE_ENABLED):
        self.enabled = enabled
        self.dir = config.CACHE_DIR / dataset_version() / namespace

    @staticmethod
    def key(*parts) -> str:
        blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str):
        if not self.enabled:
            return None
        path = self.dir / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def set(self, key: str, value) -> None:
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
