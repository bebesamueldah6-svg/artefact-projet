"""HTML/CSS components for the Streamlit app. All dataset values are HTML-escaped before rendering."""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

import streamlit as st

from edan_chat.agent.sql_guard import execute

CSS_PATH = Path(__file__).with_name("style.css")

# party colours for charts (RHDP orange, PDCI green as on their materials, others neutral tones)
PARTY_COLORS = {
    "RHDP": "#F77F00", "PDCI-RDA": "#009E60", "INDEPENDANT": "#64748B", "FPI": "#2563EB",
    "ADCI": "#9333EA", "UNPR": "#DC2626", "LE BUFFLE": "#B45309", "EDS": "#0891B2",
}
PALETTE = ["#F77F00", "#009E60", "#64748B", "#2563EB", "#9333EA", "#DC2626", "#B45309", "#0891B2",
           "#DB2777", "#65A30D"]


def inject_css() -> None:
    st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def _fr(n: float, decimals: int = 0) -> str:
    s = f"{n:,.{decimals}f}".replace(",", " ")
    return s.replace(".", ",") if decimals else s


@lru_cache(maxsize=1)
def key_figures() -> dict:
    nat = execute("SELECT inscrits, votants, taux_participation, nb_bv, bulletins_blancs FROM national_totals").df.iloc[0]
    seats = execute("SELECT party, nb_elus FROM vw_party_summary WHERE nb_elus > 0 ORDER BY nb_elus DESC").df
    hi = execute("SELECT region, taux_participation FROM vw_turnout_region ORDER BY taux_participation DESC LIMIT 1").df.iloc[0]
    lo = execute("SELECT region, taux_participation FROM vw_turnout_region ORDER BY taux_participation LIMIT 1").df.iloc[0]
    best = execute("SELECT candidate, party, vote_pct FROM vw_winners ORDER BY votes DESC LIMIT 1").df.iloc[0]
    cands = execute("SELECT COUNT(*) AS n FROM vw_results_clean").df.iloc[0]["n"]
    return {"nat": nat, "seats": seats, "hi": hi, "lo": lo, "best": best, "candidatures": int(cands)}


def hero() -> None:
    st.markdown(
        '<div class="hero"><span class="pill">Législatives 2025 · Côte d\'Ivoire</span>'
        "<h1>Discutez avec les résultats de l'EDAN 2025</h1>"
        "<p>Posez vos questions en français ou en anglais : chaque réponse vient uniquement du PDF officiel "
        "de la CEI, avec tableau, SQL, sources et graphiques.</p></div>",
        unsafe_allow_html=True)


def ticker() -> None:
    f = key_figures()
    nat, e = f["nat"], html.escape
    items = [
        f"Participation nationale <b>{_fr(nat.taux_participation, 2)} %</b>",
        f"<b>{_fr(nat.votants)}</b> votants sur <b>{_fr(nat.inscrits)}</b> inscrits",
        *[f"{e(r.party)} : <b>{int(r.nb_elus)}</b> siège{'s' if r.nb_elus > 1 else ''}" for r in f["seats"].itertuples()],
        f"Plus forte participation : <b>{e(f['hi'].region)}</b> ({_fr(f['hi'].taux_participation, 2)} %)",
        f"Plus faible participation : <b>{e(f['lo'].region)}</b> ({_fr(f['lo'].taux_participation, 2)} %)",
        f"<b>{_fr(nat.nb_bv)}</b> bureaux de vote · <b>{_fr(f['candidatures'])}</b> candidatures",
    ]
    track = "".join(f'<span class="ticker-item">{i}</span>' for i in items)
    st.markdown(
        f'<div class="ticker"><div class="label"><span class="dot"></span>EN DIRECT DU PDF</div>'
        f'<div class="ticker-track">{track}{track}</div></div>',  # duplicated for a seamless loop
        unsafe_allow_html=True)


def kpis() -> None:
    f = key_figures()
    nat, top = f["nat"], f["seats"].iloc[0]
    cards = [
        ("orange", "205", "sièges à pourvoir"),
        ("green", f"{_fr(nat.taux_participation, 2)} %", "participation"),
        ("orange", _fr(nat.votants), "votants"),
        ("green", f"{int(top.nb_elus)}", f"sièges {html.escape(top.party)}"),
        ("orange", str(len(f["seats"])), "partis représentés"),
    ]
    st.markdown('<div class="kpis">' + "".join(
        f'<div class="kpi {c}"><div class="v">{v}</div><div class="l">{label}</div></div>' for c, v, label in cards)
        + "</div>", unsafe_allow_html=True)


ROUTE_LABELS = {"sql_rules": "⚡ SQL règles", "sql_llm": "🤖 SQL généré par LLM", "rag": "🔎 recherche RAG",
                "safety": "🛡️ sécurité", "clarify": "❓ précision", "not_found": "∅ hors données"}


def meta_badges(turn) -> None:
    tokens = turn.usage.get("prompt_tokens", 0) + turn.usage.get("completion_tokens", 0)
    badges = [f'<span class="badge route-{html.escape(turn.route)}">{ROUTE_LABELS.get(turn.route, html.escape(turn.route))}</span>']
    if turn.intent:
        badges.append(f'<span class="badge neutral">{html.escape(turn.intent)}</span>')
    badges.append(f'<span class="badge neutral">{turn.latency_ms:.0f} ms</span>')
    if tokens:
        badges.append(f'<span class="badge neutral">{tokens} tokens</span>')
    badges.append(f'<span class="badge neutral">trace {html.escape(turn.trace_id)}</span>')
    st.markdown(f'<div class="meta">{"".join(badges)}</div>', unsafe_allow_html=True)


def sidebar_header(dataset_version: str | None, checks_ok: bool, llm_label: str | None) -> None:
    st.markdown('<div class="side-title">🗳️ EDAN 2025</div><div class="side-flag"></div>', unsafe_allow_html=True)
    if dataset_version:
        st.caption(f"Données `{dataset_version}` (empreinte du PDF) · "
                   + ("✅ 9 contrôles de cohérence OK" if checks_ok else "❌ contrôles en échec"))
    if llm_label:
        st.markdown(f'<div class="status ok">● LLM actif — {html.escape(llm_label)}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="status warn">● Sans LLM — règles + recherche uniquement</div>', unsafe_allow_html=True)
