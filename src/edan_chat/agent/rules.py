"""Rule-based engine (no LLM): keyword intents + entity resolution -> fixed SQL -> templated answer.

Instant and deterministic. Every intent maps to a parameterised SQL template over the curated
views, executed through the same guard as the LLM path. Unknown questions get a help message.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from edan_chat import config
from edan_chat.agent import entities
from edan_chat.agent.entities import Match
from edan_chat.agent.pipeline import Turn, write_trace
from edan_chat.agent.sql_guard import execute
from edan_chat.ingest.normalize import norm_key

HELP = (
    "Je réponds aux questions sur les résultats des législatives 2025 (EDAN 2025). Par exemple :\n"
    "- « Combien de sièges a obtenu chaque parti ? »\n"
    "- « Qui a gagné à Yopougon ? » · « Résultats à Cocody »\n"
    "- « Taux de participation à Bouaké » · « Participation par région »\n"
    "- « Les 5 circonscriptions avec la plus faible participation »\n"
    "- « Combien de sièges pour le PDCI dans le Poro ? » · « Score de Koffi Aka Charles »"
)


# ---- formatting -----------------------------------------------------------------------------
def n(x) -> str:
    return f"{int(x):,}".replace(",", " ")


def pct(x) -> str:
    return f"{float(x):.2f}".replace(".", ",") + " %"


def pl(x, word: str) -> str:
    """pl(155, 'siège') -> '155 sièges'"""
    return f"{n(x)} {word}{'s' if int(x) > 1 else ''}"


def lst(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1]


def sq(values) -> str:
    """SQL list of quoted literals; values come from the database index, quotes are escaped."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


# ---- question analysis ----------------------------------------------------------------------
@dataclass
class Q:
    text: str                  # normalized question (upper, no accents)
    circ: list[Match]
    regions: list[Match]
    parties: list[Match]
    candidates: list[Match]

    def has(self, *patterns: str) -> bool:
        return any(re.search(p, self.text) for p in patterns)

    @property
    def circ_ids(self) -> list[str]:
        return sorted({c for m in self.circ for c in m.circ_ids})

    @property
    def top_n(self) -> int:
        m = re.search(r"\b(\d{1,3})\b", self.text)
        return max(1, min(int(m.group(1)), 50)) if m else (1 if self.has(r"\bLA PLUS\b", r"\bLE PLUS\b",
                                                                          r"\bLE MEILLEUR\b") else 10)

    @property
    def ascending(self) -> bool:
        return self.has(r"FAIBLE", r"BASSE", r"\bMOINS\b", r"PIRE", r"MINIMUM", r"BAS\b")


def analyse(question: str, db_path) -> Q:
    ms = entities.resolve(question, db_path)
    q = Q(norm_key(question), [], [], [], [])
    for m in ms:
        {"circonscription": q.circ, "region": q.regions, "party": q.parties,
         "candidate": q.candidates}[m.kind].append(m)
    # a name that is both a locality and a region: the word "région" decides
    if q.circ and q.regions:
        if q.has(r"\bREGION"):
            q.circ = [m for m in q.circ if m.mention not in {r.mention for r in q.regions}]
        else:
            q.regions = [m for m in q.regions if m.mention not in {c.mention for c in q.circ}]
    return q


TURNOUT = (r"PARTICIPATION", r"\bVOTANTS?\b", r"INSCRITS?", r"ABSTENTION", r"\bNULS?\b",
           r"BLANCS?", r"EXPRIMES?", r"BUREAUX? DE VOTE")
WIN = (r"GAGN", r"\bELUE?S?\b", r"VAINQU", r"REMPORT", r"DEPUTE", r"SIEGES?", r"\bEN TETE\b")
RESULTS = (r"RESULTAT", r"SCORES?", r"\bVOIX\b", r"CANDIDAT", r"\bVOTES?\b", r"DETAIL")
RANK = (r"PLUS FORTE", r"PLUS FAIBLE", r"PLUS ELEVE", r"PLUS BASSE", r"MEILLEUR", r"\bTOP\b",
        r"CLASSEMENT", r"\bLES \d+", r"PLUS GRAND", r"PLUS PETIT", r"MOINS")
OUT_OF_SCOPE = (r"PRESIDENTIEL", r"MUNICIPAL", r"SENATORIAL", r"REGIONALES", r"REFERENDUM",
                r"\b(19|20)(?!25\b)\d\d\b", r"PREDI", r"PREVOI", r"SONDAGE", r"VA GAGNER", r"POURQUOI")
FORBIDDEN = (r"SUPPRIM", r"EFFAC", r"\bDELETE\b", r"\bDROP\b", r"MODIFI", r"\bUPDATE\b",
             r"\bINSERT\b", r"AJOUT")


TOPIC_WORDS = (*TURNOUT, *WIN, *RESULTS, *RANK, r"PARTIS?", r"REPARTITION", r"COMBIEN")


# ---- intents --------------------------------------------------------------------------------
# each returns (sql, answer_builder) or None when it does not apply
Plan = tuple[str, Callable[[pd.DataFrame], str]]


def i_candidate(q: Q) -> Plan | None:
    if not q.candidates:
        return None
    names = [m.value for m in q.candidates]
    sql = ("SELECT candidate, party, circonscription, votes, vote_pct, rank_in_circ, is_elected, "
           f"source_page FROM vw_results_clean WHERE candidate IN ({sq(names)}) ORDER BY votes DESC")

    def say(df):
        return "\n".join(
            f"- **{r.candidate}** ({r.party}) à {r.circonscription} : {n(r.votes)} voix "
            f"({pct(r.vote_pct)}), {'**élu(e)**' if r.is_elected else f'classé(e) {r.rank_in_circ}ᵉ'}."
            for r in df.itertuples())
    return sql, say


def i_turnout_place(q: Q) -> Plan | None:
    if not (q.has(*TURNOUT) and (q.circ or q.regions)):
        return None
    if q.circ:
        sql = ("SELECT circonscription, region, taux_participation, inscrits, votants, bulletins_nuls, "
               "bulletins_blancs, suffrages_exprimes, source_page FROM vw_turnout "
               f"WHERE circ_id IN ({sq(q.circ_ids)}) ORDER BY circ_id")
    else:
        sql = ("SELECT region, taux_participation, inscrits, votants, bulletins_nuls, bulletins_blancs, "
               f"nb_circonscriptions FROM vw_turnout_region WHERE region IN ({sq(m.value for m in q.regions)})")

    def say(df):
        label = "circonscription" if q.circ else "region"
        return "\n".join(
            f"- **{getattr(r, label)}** : participation de **{pct(r.taux_participation)}** "
            f"({n(r.votants)} votants sur {n(r.inscrits)} inscrits)."
            for r in df.itertuples())
    return sql, say


def i_turnout_ranking(q: Q) -> Plan | None:
    if not (q.has(*TURNOUT) and (q.has(*RANK) or q.has(r"PAR REGION", r"PAR CIRCONSCRIPTION"))):
        return None
    order = "ASC" if q.ascending else "DESC"
    by_region = q.has(r"REGION")
    if by_region:
        limit = "" if q.has(r"PAR REGION") and not q.has(*RANK) else f" LIMIT {q.top_n}"
        sql = ("SELECT region, taux_participation, inscrits, votants FROM vw_turnout_region "
               f"ORDER BY taux_participation {order}{limit}")
    else:
        sql = ("SELECT circonscription, region, taux_participation, inscrits, votants, source_page "
               f"FROM vw_turnout ORDER BY taux_participation {order} LIMIT {q.top_n}")

    def say(df):
        what = "région" if by_region else "circonscription"
        sens = "la plus faible" if order == "ASC" else "la plus forte"
        if len(df) == 1:
            r = df.iloc[0]
            return (f"La {what} avec {sens} participation est **{r.iloc[0]}** : "
                    f"**{pct(r.taux_participation)}** ({n(r.votants)} votants sur {n(r.inscrits)} inscrits).")
        head = (f"Participation par {what} ({'croissante' if order == 'ASC' else 'décroissante'}) :"
                if not q.has(*RANK) else f"Les {len(df)} {what}s avec {sens} participation :")
        return head + "\n" + "\n".join(f"{i}. {r.iloc[0]} : {pct(r.taux_participation)}"
                                       for i, (_, r) in enumerate(df.iterrows(), 1))
    return sql, say


def i_turnout_national(q: Q) -> Plan | None:
    if not q.has(*TURNOUT):
        return None
    sql = ("SELECT inscrits, votants, taux_participation, bulletins_nuls, bulletins_blancs, "
           "suffrages_exprimes, nb_bv, source_page FROM national_totals")

    def say(df):
        r = df.iloc[0]
        return (f"Au niveau national : **{n(r.inscrits)} inscrits**, **{n(r.votants)} votants**, soit une "
                f"participation de **{pct(r.taux_participation)}**. {n(r.suffrages_exprimes)} suffrages "
                f"exprimés, {n(r.bulletins_nuls)} bulletins nuls et {n(r.bulletins_blancs)} blancs, "
                f"dans {n(r.nb_bv)} bureaux de vote.")
    return sql, say


def i_party_in_area(q: Q) -> Plan | None:
    if not (q.parties and (q.circ or q.regions)):
        return None
    where = (f"circ_id IN ({sq(q.circ_ids)})" if q.circ
             else f"region IN ({sq(m.value for m in q.regions)})")
    area = lst([m.value for m in (q.circ or q.regions)])
    pks = [m.value for m in q.parties]
    # aggregate without GROUP BY: always one row, so "0 élu" is answered explicitly
    sql = ("SELECT COUNT(*) AS nb_candidatures, "
           "COALESCE(SUM(CASE WHEN is_elected THEN 1 ELSE 0 END), 0) AS nb_elus, "
           "COALESCE(SUM(votes), 0) AS total_votes FROM vw_results_clean "
           f"WHERE {where} AND party_key IN ({sq(pks)})")

    def say(df):
        r, who = df.iloc[0], lst([m.mention for m in q.parties])
        if r.nb_candidatures == 0:
            return f"**{who}** n'avait aucune candidature à {area}."
        return (f"**{who}** à {area} : **{pl(r.nb_elus, 'élu')}** pour "
                f"{pl(r.nb_candidatures, 'candidature')}, {n(r.total_votes)} voix.")
    return sql, say


def i_party(q: Q) -> Plan | None:
    if not q.parties:
        return None
    sql = ("SELECT party, nb_elus, nb_candidatures, total_votes, vote_share_pct FROM vw_party_summary "
           f"WHERE party_key IN ({sq(m.value for m in q.parties)}) ORDER BY nb_elus DESC")

    def say(df):
        return "\n".join(f"- **{r.party}** : **{pl(r.nb_elus, 'siège')}** sur 205, "
                         f"{pl(r.nb_candidatures, 'candidature')}, {n(r.total_votes)} voix "
                         f"({pct(r.vote_share_pct)} des voix)." for r in df.itertuples())
    return sql, say


def i_winners_area(q: Q) -> Plan | None:
    if not ((q.circ or q.regions) and (q.has(*WIN) or not q.has(*RESULTS))):
        return None
    if q.circ:
        sql = ("SELECT circonscription, candidate, party, votes, vote_pct, source_page FROM vw_winners "
               f"WHERE circ_id IN ({sq(q.circ_ids)}) ORDER BY circ_id")

        def say(df):
            return "\n".join(f"- **{r.circonscription}** : **{r.candidate}** ({r.party}) élu(e) avec "
                             f"{n(r.votes)} voix ({pct(r.vote_pct)})." for r in df.itertuples())
        return sql, say
    sql = ("SELECT region, party, COUNT(*) AS nb_elus FROM vw_winners "
           f"WHERE region IN ({sq(m.value for m in q.regions)}) GROUP BY region, party "
           "ORDER BY region, nb_elus DESC")

    def say(df):
        out = []
        for region, g in df.groupby("region", sort=False):
            parts = [f"{r.party} {n(r.nb_elus)}" for r in g.itertuples()]
            out.append(f"- **{region}** ({n(g.nb_elus.sum())} sièges) : {', '.join(parts)}.")
        return "\n".join(out)
    return sql, say


def i_results_area(q: Q) -> Plan | None:
    if not q.circ:
        return None
    sql = ("SELECT circonscription, candidate, party, votes, vote_pct, rank_in_circ, is_elected, source_page "
           f"FROM vw_results_clean WHERE circ_id IN ({sq(q.circ_ids)}) ORDER BY circ_id, rank_in_circ")

    def say(df):
        out = []
        for circ, g in df.groupby("circonscription", sort=False):
            out.append(f"**{circ}** ({len(g)} candidatures) :")
            out += [f"{r.rank_in_circ}. {r.candidate} ({r.party}) : {n(r.votes)} voix, {pct(r.vote_pct)}"
                    + (" — **élu(e)**" if r.is_elected else "") for r in g.head(10).itertuples()]
        return "\n".join(out)
    return sql, say


def i_best_score(q: Q) -> Plan | None:
    if not (q.has(*RANK) and q.has(r"POURCENTAGE", r"SCORE", r"\bVOIX\b", r"\bELUE?S?\b", r"CANDIDAT")):
        return None
    col = "vote_pct" if q.has(r"POURCENTAGE", r"%") or not q.has(r"\bVOIX\b") else "votes"
    view = "vw_winners" if q.has(r"\bELUE?S?\b") or not q.ascending else "vw_results_clean"
    order = "ASC" if q.ascending else "DESC"
    sql = (f"SELECT candidate, party, circonscription, votes, vote_pct, source_page FROM {view} "
           f"ORDER BY {col} {order} LIMIT {q.top_n}")

    def say(df):
        return "\n".join(f"{i}. **{r.candidate}** ({r.party}, {r.circonscription}) : {n(r.votes)} voix, "
                         f"{pct(r.vote_pct)}" for i, r in enumerate(df.itertuples(), 1))
    return sql, say


def i_seats(q: Q) -> Plan | None:
    if not (q.has(r"SIEGES?", r"\bELUS?\b", r"PARTIS?", r"REPARTITION", r"ASSEMBLEE", r"MAJORITE")):
        return None
    sql = ("SELECT party, nb_elus, nb_candidatures, total_votes, vote_share_pct FROM vw_party_summary "
           "WHERE nb_elus > 0 ORDER BY nb_elus DESC, total_votes DESC")

    def say(df):
        lines = [f"- **{r.party}** : {pl(r.nb_elus, 'siège')} ({pct(r.vote_share_pct)} des voix)"
                 for r in df.itertuples()]
        return "Répartition des 205 sièges :\n" + "\n".join(lines)
    return sql, say


def i_count(q: Q) -> Plan | None:
    if not q.has(r"COMBIEN DE (CIRCONSCRIPTIONS|CANDIDA|PARTIS|REGIONS)"):
        return None
    sql = ("SELECT COUNT(DISTINCT circ_id) AS circonscriptions, COUNT(*) AS candidatures, "
           "COUNT(DISTINCT party) AS partis, COUNT(DISTINCT region) AS regions FROM vw_results_clean")

    def say(df):
        r = df.iloc[0]
        return (f"**{n(r.circonscriptions)} circonscriptions**, **{n(r.candidatures)} candidatures** "
                f"de **{n(r.partis)} partis ou groupements** (dont les indépendants), "
                f"réparties dans **{n(r.regions)} régions et districts**.")
    return sql, say


# order matters: most specific first
INTENTS = [i_candidate, i_count, i_turnout_ranking, i_turnout_place, i_turnout_national,
           i_party_in_area, i_party, i_best_score, i_winners_area, i_results_area, i_seats]


# ---- engine ---------------------------------------------------------------------------------
class RuleAgent:
    model = "règles"

    def __init__(self, db_path=config.DB_PATH, trace_dir=config.TRACE_DIR):
        self.db_path, self.trace_dir = db_path, trace_dir

    def ask(self, question: str, history: list[Turn] | None = None) -> Turn:
        t0 = time.perf_counter()
        turn = Turn(question=question.strip())
        q = analyse(turn.question, self.db_path)
        turn.hints = [m.hint() for m in q.circ + q.regions + q.parties + q.candidates]

        if q.has(*FORBIDDEN):
            turn.kind, turn.text = "refuse", "Les données sont en lecture seule : je ne peux rien modifier ni supprimer."
        elif q.has(*OUT_OF_SCOPE):
            turn.kind, turn.text = "refuse", (
                "Je ne peux répondre qu'à partir des résultats officiels des législatives 2025 "
                "(EDAN 2025) publiés par la CEI : pas d'autres scrutins, ni prévisions, ni opinions.")
        else:
            plan = self._plan(q, history or [])
            if plan is None:
                turn.kind, turn.text = "clarify", "Je n'ai pas compris la question.\n\n" + HELP
            else:
                sql, say = plan
                result = execute(sql, self.db_path)
                turn.sql, turn.df, turn.truncated = result.sql, result.df, result.truncated
                turn.attempts = [{"sql": sql, "error": None}]
                if "source_page" in result.df.columns:
                    turn.source_pages = sorted({int(p) for p in result.df["source_page"].dropna()})
                turn.text = say(result.df) if not result.df.empty else \
                    "Aucun résultat ne correspond dans les données. Essayez de reformuler."
        turn.elapsed_s = round(time.perf_counter() - t0, 3)
        write_trace(turn, self.trace_dir, self.model)
        return turn

    def _plan(self, q: Q, history: list[Turn]) -> Plan | None:
        # follow-up such as "et à Abobo ?": entities but no topic word -> reuse previous keywords
        previous = next((t for t in reversed(history) if t.kind == "answer"), None)
        if previous and not q.has(*TOPIC_WORDS) and (q.circ or q.regions or q.parties):
            q.text = norm_key(previous.question) + " " + q.text
        return next((plan for intent in INTENTS if (plan := intent(q))), None)
