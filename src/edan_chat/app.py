"""Streamlit chat UI:  `uv run streamlit run src/edan_chat/app.py`"""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

from edan_chat import config
from edan_chat.agent.llm import OllamaChat
from edan_chat.agent.pipeline import Agent, Turn

EXAMPLES = [
    "Combien de sièges a obtenu chaque parti ?",
    "Qui a gagné à Yopougon ?",
    "Quelles sont les 10 circonscriptions avec la plus forte participation ?",
    "Taux de participation par région",
    "Combien d'indépendants ont été élus ?",
]

st.set_page_config(page_title="EDAN 2025 — Résultats des législatives", page_icon="🗳️", layout="wide")


@st.cache_resource
def get_agent() -> Agent:
    return Agent()


def manifest() -> dict:
    try:
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def chart(df: pd.DataFrame):
    """A bar chart when the result is one label column + one numeric column of a sensible size."""
    if df is None or not 2 <= len(df) <= 40:
        return None
    labels = [c for c in df.columns if df[c].dtype == object and c not in ("party_key",)]
    nums = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
            and c not in ("source_page", "row_id", "rank_in_circ")]
    if not labels or not nums:
        return None
    y = next((c for c in ("nb_elus", "taux_participation", "votes", "vote_pct", "total_votes")
              if c in nums), nums[0])
    fig = px.bar(df, x=labels[0], y=y, color=labels[1] if len(labels) > 1 and labels[1] == "party" else None)
    fig.update_layout(height=380, margin={"l": 0, "r": 0, "t": 10, "b": 0}, xaxis_title=None)
    return fig


def render(turn: Turn) -> None:
    icon = {"refuse": "🚫 ", "clarify": "❓ ", "error": "⚠️ "}.get(turn.kind, "")
    st.markdown(icon + turn.text)
    if turn.df is None:
        return
    fig = chart(turn.df)
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)
    with st.expander(f"Données ({len(turn.df)} lignes{', tronquées' if turn.truncated else ''})"
                     + (f" — PDF p. {', '.join(map(str, turn.source_pages[:10]))}" if turn.source_pages else "")):
        st.dataframe(turn.df, use_container_width=True, hide_index=True)
        st.code(turn.sql, language="sql")
        if len(turn.attempts) > 1:
            st.caption(f"Requête corrigée après {len(turn.attempts) - 1} erreur(s).")
        st.caption(f"{turn.elapsed_s:.1f} s")


# ---- sidebar ---------------------------------------------------------------------------------
m = manifest()
with st.sidebar:
    st.header("🗳️ EDAN 2025")
    st.write("Questions-réponses sur les résultats officiels des élections législatives "
             "ivoiriennes 2025, publiés par la CEI.")
    if m:
        ok = all(v["passed"] for v in m["validation"].values())
        st.caption(f"Données : `{m['pdf_file']}` · version `{m['dataset_version']}` · "
                   f"{'✅ contrôles de cohérence OK' if ok else '❌ contrôles en échec'}")
    llm = OllamaChat()
    if llm.available():
        st.success(f"Modèle local : {llm.model}")
    else:
        st.error(f"Modèle `{llm.model}` indisponible sur {llm.host}. "
                 f"Lancez Ollama puis `ollama pull {llm.model}`.")
    st.subheader("Exemples")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state.pending = ex
    if st.button("Effacer la conversation", type="secondary"):
        st.session_state.history = []

# ---- chat ------------------------------------------------------------------------------------
st.session_state.setdefault("history", [])
for past in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(past.question)
    with st.chat_message("assistant"):
        render(past)

question = st.chat_input("Posez votre question sur les résultats…") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Analyse en cours…"):
            turn = get_agent().ask(question, st.session_state.history)
        render(turn)
    st.session_state.history.append(turn)
