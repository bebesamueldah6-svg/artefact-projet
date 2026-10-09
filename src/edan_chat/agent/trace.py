"""End-to-end request tracing: one JSON line per request with timed spans.

A trace records routing, entity resolution, retrieval hits, SQL + validation outcome, LLM calls
(with token usage), chart generation and total latency. Stored in TRACE_DIR/YYYY-MM-DD.jsonl.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class Tracer:
    def __init__(self, question: str, dataset_version: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.question = question
        self.dataset_version = dataset_version
        self.spans: list[dict] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}
        self._t0 = time.perf_counter()

    @contextmanager
    def span(self, name: str, **attrs):
        record = {"name": name, **attrs}
        t0 = time.perf_counter()
        try:
            yield record
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            record["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.spans.append(record)

    def add_usage(self, usage: dict | None) -> None:
        self.usage["llm_calls"] += 1
        for k in ("prompt_tokens", "completion_tokens"):
            self.usage[k] += int((usage or {}).get(k) or 0)

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def to_dict(self, **extra) -> dict:
        return {"trace_id": self.id, "ts": datetime.now(UTC).isoformat(), "question": self.question,
                "dataset_version": self.dataset_version, "latency_ms": self.elapsed_ms,
                "usage": self.usage, "spans": self.spans, **extra}

    def write(self, trace_dir: Path, **extra) -> dict:
        record = self.to_dict(**extra)
        trace_dir.mkdir(parents=True, exist_ok=True)
        with open(trace_dir / f"{datetime.now(UTC):%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record
