"""Shared data structures: one Turn per question, one Session per chat."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

NOT_FOUND = "Not found in the provided PDF dataset."


@dataclass
class Turn:
    question: str
    lang: str = "fr"
    kind: str = "answer"        # answer | clarify | refuse | not_found | error
    route: str = ""             # safety | clarify | sql_rules | sql_llm | rag | not_found
    intent: str = ""            # e.g. seats_by_party, turnout_ranking, chart, lookup...
    text: str = ""
    sql: str | None = None
    df: pd.DataFrame | None = None
    truncated: bool = False
    chart: dict | None = None   # {"type": bar|pie|histogram, "x": col, "y": col|None, "title": str}
    citations: list[dict] = field(default_factory=list)  # {source_page, row_id, excerpt}
    options: list[dict] = field(default_factory=list)    # clarification choices {label, value}
    hints: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    trace_id: str = ""
    latency_ms: float = 0.0
    usage: dict = field(default_factory=dict)

    @property
    def elapsed_s(self) -> float:
        return round(self.latency_ms / 1000, 3)


@dataclass
class Session:
    history: list[Turn] = field(default_factory=list)
    # disambiguation memory: normalized mention -> chosen circ_ids (or region label)
    memory: dict[str, dict] = field(default_factory=dict)
    # clarification waiting for the user's choice
    pending: dict | None = None
