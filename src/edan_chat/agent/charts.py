"""Chart requests: detect the requested chart type and build a declarative spec from a result.

The spec ({type, x, y, title}) is rendered by the UI with Plotly — no model-generated code is
ever executed.
"""

from __future__ import annotations

import re

import pandas as pd

from edan_chat.ingest.normalize import norm_key

_PIE = (r"\bPIE\b", r"CAMEMBERT", r"SECTEURS?", r"DONUT", r"PROPORTION", r"PART DES")
_HIST = (r"HISTOGRAM", r"DISTRIBUTION", r"REPARTITION DES (TAUX|SCORES|VOIX)")
_BAR = (r"\bBARS?\b", r"BARRES?", r"BATONS?", r"\bCHART\b", r"GRAPHIQUE", r"GRAPHE", r"\bPLOT\b",
        r"DIAGRAMME", r"VISUALI", r"COURBE", r"\bGRAPH\b")

_PRIORITY_Y = ("nb_elus", "seats", "taux_participation", "votes", "vote_pct", "total_votes",
               "vote_share_pct", "inscrits", "votants", "nb_candidatures")
_NOT_Y = ("source_page", "row_id", "rank_in_circ", "circ_id")


def requested_type(question: str) -> str | None:
    q = norm_key(question)
    if any(re.search(p, q) for p in _PIE):
        return "pie"
    if any(re.search(p, q) for p in _HIST):
        return "histogram"
    if any(re.search(p, q) for p in _BAR):
        return "bar"
    return None


def build_spec(df: pd.DataFrame | None, chart_type: str | None, title: str = "",
               x: str | None = None, y: str | None = None) -> dict | None:
    """Pick sensible axes; returns None when the result cannot be charted."""
    if df is None or df.empty or not chart_type:
        return None
    labels = [c for c in df.columns if df[c].dtype == object and c not in ("party_key", "excerpt", "circ_id")]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
            and not pd.api.types.is_bool_dtype(df[c]) and c not in _NOT_Y]
    y = y if y in df.columns else next((c for c in _PRIORITY_Y if c in nums), nums[0] if nums else None)
    x = x if x in df.columns else (labels[0] if labels else None)
    if chart_type == "histogram" and y and len(df) >= 8:
        return {"type": "histogram", "x": y, "y": None, "title": title}
    if not (x and y) or len(df) < 2:
        return None
    if chart_type == "pie" and len(df) > 15:
        chart_type = "bar"  # unreadable pie
    # a "histogram" of a categorical breakdown (e.g. winners by party) is a bar chart of counts
    return {"type": "pie" if chart_type == "pie" else "bar", "x": x, "y": y, "title": title}
