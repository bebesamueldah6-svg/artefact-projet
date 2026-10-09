"""Interactive dashboard tab: filters, live KPIs, clickable charts with drill-down.

Data comes from the curated views through the SQL guard (fixed queries, no user SQL); filtering is
done in pandas on the 205 constituencies.
"""

from __future__ import annotations

import html

import pandas as pd
import plotly.express as px
import streamlit as st

from edan_chat.agent.intents import sq
from edan_chat.agent.sql_guard import execute
from edan_chat.ui.components import PALETTE, PARTY_COLORS

LAYOUT = {"paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
          "font": {"family": "sans-serif", "color": "#1F2937"}, "margin": {"l": 0, "r": 0, "t": 40, "b": 0}}
COLORS = {"color_discrete_map": PARTY_COLORS, "color_discrete_sequence": PALETTE}


@st.cache_data(show_spinner=False)
def load_constituencies() -> pd.DataFrame:
    """One row per constituency: turnout + winner."""
    df = execute(
        "SELECT t.circ_id, t.circonscription, t.region, t.inscrits, t.votants, t.taux_participation, "
        "t.bulletins_nuls, t.bulletins_blancs, t.suffrages_exprimes, t.source_page, "
        "w.party, w.candidate, w.votes, w.vote_pct "
        "FROM vw_turnout t JOIN vw_winners w USING (circ_id) ORDER BY t.circ_id").df
    df["label"] = df["circ_id"] + " · " + df["circonscription"].str.slice(0, 45)
    return df


def _fr(n: float, d: int = 0) -> str:
    s = f"{n:,.{d}f}".replace(",", " ")
    return s.replace(".", ",") if d else s


def _kpis(df: pd.DataFrame) -> None:
    turnout = 100 * df["votants"].sum() / df["inscrits"].sum() if len(df) else 0
    top = df["party"].value_counts()
    cards = [("orange", _fr(len(df)), "circonscriptions / sièges"),
             ("green", f"{_fr(turnout, 2)} %", "participation (sélection)"),
             ("orange", _fr(df["votants"].sum()), "votants"),
             ("green", _fr(df["inscrits"].sum()), "inscrits"),
             ("orange", f"{html.escape(top.index[0])} {top.iloc[0]}" if len(top) else "—", "1er parti (sièges)")]
    st.markdown('<div class="kpis">' + "".join(
        f'<div class="kpi {c}"><div class="v">{v}</div><div class="l">{lab}</div></div>' for c, v, lab in cards)
        + "</div>", unsafe_allow_html=True)


def _selected(event, field: str) -> list:
    try:
        return [p.get(field) for p in event.selection.points if p.get(field) is not None]
    except AttributeError:
        return []


def render_dashboard() -> None:
    data = load_constituencies()

    # ---- filters ---------------------------------------------------------------------------
    f1, f2, f3 = st.columns([3, 3, 2])
    regions = f1.multiselect("Régions", sorted(data["region"].unique()), placeholder="Toutes les régions")
    parties = f2.multiselect("Parti du vainqueur", sorted(data["party"].unique()), placeholder="Tous les partis")
    lo, hi = f3.slider("Participation (%)", 0, 100, (0, 100))
    df = data
    if regions:
        df = df[df["region"].isin(regions)]
    if parties:
        df = df[df["party"].isin(parties)]
    df = df[df["taux_participation"].between(lo, hi)]
    if df.empty:
        st.info("Aucune circonscription ne correspond à ces filtres.")
        return
    _kpis(df)

    # ---- seats by party + turnout by region --------------------------------------------------
    c1, c2 = st.columns(2)
    with c1:
        kind = st.segmented_control("Sièges par parti", ["Barres", "Anneau"], default="Anneau", key="seat_kind")
        seats = df.groupby("party", as_index=False).size().rename(columns={"size": "sieges"}).sort_values(
            "sieges", ascending=False)
        if kind == "Barres":
            fig = px.bar(seats, x="party", y="sieges", color="party", text_auto=True, **COLORS)
            fig.update_layout(showlegend=False, xaxis_title=None, yaxis_title="sièges")
        else:
            fig = px.pie(seats, names="party", values="sieges", hole=0.5, color="party", **COLORS)
            fig.update_traces(textinfo="value+label", marker={"line": {"color": "#fff", "width": 2}})
        fig.update_layout(height=380, title=f"Répartition des {len(df)} sièges", **LAYOUT)
        st.plotly_chart(fig, width="stretch", key="seats_chart")
    with c2:
        reg = (df.groupby("region", as_index=False)[["votants", "inscrits"]].sum()
               .assign(participation=lambda d: (100 * d.votants / d.inscrits).round(2))
               .sort_values("participation"))
        fig = px.bar(reg, x="participation", y="region", orientation="h", text="participation",
                     color="participation", color_continuous_scale=["#FDE7CF", "#F77F00", "#009E60"],
                     custom_data=["region"])
        fig.update_layout(height=max(380, 18 * len(reg) + 80), title="Participation par région — cliquez une barre",
                          coloraxis_showscale=False, yaxis_title=None, xaxis_title="%", **LAYOUT)
        event = st.plotly_chart(fig, width="stretch", key="region_chart", on_select="rerun",
                                selection_mode="points")
    clicked_regions = _selected(event, "y")

    # ---- scatter: turnout vs winner score -----------------------------------------------------
    st.markdown("#### Participation × score du vainqueur — cliquez un point pour le détail")
    scope = df[df["region"].isin(clicked_regions)] if clicked_regions else df
    if clicked_regions:
        st.caption(f"Filtré sur : {', '.join(clicked_regions)} (cliquez ailleurs ou sur une autre barre pour changer)")
    fig = px.scatter(scope, x="taux_participation", y="vote_pct", size="inscrits", color="party",
                     hover_name="circonscription", custom_data=["circ_id"], size_max=38,
                     hover_data={"candidate": True, "region": True, "votes": ":,", "inscrits": ":,",
                                 "taux_participation": ":.2f", "vote_pct": ":.2f"},
                     labels={"taux_participation": "participation (%)", "vote_pct": "score du vainqueur (%)"},
                     **COLORS)
    fig.update_traces(marker={"line": {"color": "#fff", "width": 1}, "opacity": 0.85})
    fig.update_layout(height=460, legend_title_text="parti", **LAYOUT)
    fig.update_xaxes(gridcolor="#F3E3CF")
    fig.update_yaxes(gridcolor="#F3E3CF")
    event = st.plotly_chart(fig, width="stretch", key="scatter_chart", on_select="rerun",
                            selection_mode=("points", "box", "lasso"))
    picked = [c[0] if isinstance(c, list) else c for c in _selected(event, "customdata")]

    # ---- drill-down -------------------------------------------------------------------------
    if picked:
        _detail(picked[:3], data)
    else:
        st.caption("Astuce : cliquez un point (ou entourez-en plusieurs au lasso) pour voir les résultats détaillés.")

    with st.expander(f"📋 Tableau des {len(scope)} circonscriptions"):
        search = st.text_input("Rechercher une circonscription, un candidat…", key="dash_search")
        table = scope
        if search:
            mask = scope[["circonscription", "candidate", "region", "party"]].apply(
                lambda col: col.str.contains(search, case=False, na=False)).any(axis=1)
            table = scope[mask]
        st.dataframe(table.drop(columns=["label"]), width="stretch", hide_index=True,
                     column_config={"taux_participation": st.column_config.ProgressColumn(
                         "participation", min_value=0, max_value=100, format="%.2f %%"),
                         "vote_pct": st.column_config.NumberColumn("score vainqueur", format="%.2f %%")})


def _detail(circ_ids: list[str], data: pd.DataFrame) -> None:
    res = execute("SELECT circ_id, circonscription, candidate, party, votes, vote_pct, is_elected, source_page "
                  f"FROM vw_results_clean WHERE circ_id IN ({sq(circ_ids)}) ORDER BY circ_id, votes DESC").df
    for cid in circ_ids:
        row = data[data["circ_id"] == cid].iloc[0]
        r = res[res["circ_id"] == cid]
        st.markdown(f"#### 📍 {row.circonscription} ({cid}) — {row.region}")
        a, b = st.columns([2, 3])
        with a:
            st.markdown(
                f"- **Élu(e)** : {row.candidate} ({row.party}), {_fr(row.vote_pct, 2)} %\n"
                f"- **Participation** : {_fr(row.taux_participation, 2)} % ({_fr(row.votants)} / {_fr(row.inscrits)})\n"
                f"- **Nuls / blancs** : {_fr(row.bulletins_nuls)} / {_fr(row.bulletins_blancs)}\n"
                f"- **Source** : PDF p. {int(row.source_page)}")
        with b:
            fig = px.bar(r.head(10).iloc[::-1], x="votes", y="candidate", color="party", orientation="h",
                         text="vote_pct", **COLORS)
            fig.update_traces(texttemplate="%{text:.1f} %", textposition="outside")
            fig.update_layout(height=60 + 34 * min(len(r), 10), showlegend=True, yaxis_title=None,
                              legend_title_text=None, **LAYOUT)
            st.plotly_chart(fig, width="stretch", key=f"detail_{cid}")
