"""Deterministic SQL path: keyword intents + resolved entities -> parameterised SQL -> templated answer.

Each intent returns a Plan (intent name, intent family, SQL over the curated views, answer
builder, default chart axes). SQL still goes through the guard. Answers are built only from the
returned rows, in the language of the question.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from edan_chat.agent.entities import Match
from edan_chat.agent.lang import t
from edan_chat.ingest.normalize import norm_key


# ---- formatting -------------------------------------------------------------------------------
def fmt_n(x, lang: str) -> str:
    s = f"{int(x):,}"
    return s.replace(",", " ") if lang == "fr" else s


def fmt_pct(x, lang: str) -> str:
    return f"{float(x):.2f}".replace(".", ",") + " %" if lang == "fr" else f"{float(x):.2f}%"


def plural(x, lang: str, fr: str, en: str) -> str:
    word = fr if lang == "fr" else en
    if int(x) > 1:
        word = word[:-1] + "ies" if word.endswith("y") and lang == "en" else word + "s"
    return f"{fmt_n(x, lang)} {word}"


def join_and(items: list[str], lang: str) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + t(lang, " et ", " and ") + items[-1]


def sq(values) -> str:
    """SQL list of quoted literals (values come from the database index; quotes are escaped)."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


# ---- keywords (normalized: upper case, no accents) ----------------------------------------------
TURNOUT = (r"PARTICIPATION", r"TURNOUT", r"\bVOTANTS?\b", r"\bVOTERS\b", r"INSCRITS?", r"REGISTERED",
           r"ABSTENTION", r"\bNULS?\b", r"INVALID", r"SPOILED", r"BLANCS?\b", r"\bBLANK", r"EXPRIMES?",
           r"BUREAUX? DE VOTE", r"POLLING")
WIN = (r"GAGN", r"\bWON\b", r"\bWINS?\b", r"WINNERS?", r"\bELUE?S?\b", r"ELECTED", r"VAINQU", r"REMPORT",
       r"DEPUTE", r"SIEGES?", r"\bSEATS?\b", r"EN TETE", r"\bMPS?\b")
RESULTS = (r"RESULTAT", r"RESULTS?", r"SCORES?", r"\bVOIX\b", r"\bVOTES?\b", r"CANDIDAT", r"CANDIDATES?",
           r"DETAIL", r"BREAKDOWN")
RANK = (r"PLUS FORTE", r"PLUS FAIBLE", r"PLUS ELEVE", r"PLUS BASSE", r"MEILLEUR", r"\bTOP\b", r"CLASSEMENT",
        r"RANKING", r"\bLES \d+", r"PLUS GRAND", r"PLUS PETIT", r"\bMOINS\b", r"HIGHEST", r"LOWEST",
        r"\bBEST\b", r"WORST", r"\bMOST\b", r"\bLEAST\b", r"BIGGEST", r"LARGEST", r"SMALLEST", r"\bRANK")
BY_REGION = (r"PAR REGION", r"BY REGION", r"PER REGION", r"EACH REGION", r"CHAQUE REGION", r"PAR DISTRICT")
BY_CIRC = (r"PAR CIRCONSCRIPTION", r"BY CONSTITUENCY", r"PER CONSTITUENCY", r"EACH CONSTITUENCY")
PARTY_WORDS = (r"PARTIS?\b", r"PARTY", r"PARTIES", r"REPARTITION", r"ASSEMBLEE", r"ASSEMBLY", r"MAJORIT")
COUNT = (r"COMBIEN DE (CIRCONSCRIPTIONS|CANDIDA|PARTIS|REGIONS)",
         r"HOW MANY (CONSTITUENCIES|CANDIDA|PARTIES|REGIONS)", r"NOMBRE DE (CIRCONSCRIPTIONS|CANDIDA)",
         r"NUMBER OF (CONSTITUENCIES|CANDIDA)")
TOPIC_WORDS = (*TURNOUT, *WIN, *RESULTS, *RANK, *PARTY_WORDS, r"COMBIEN", r"HOW MANY")
# modifiers the fixed templates do not model: such questions go to the LLM text-to-SQL path
COMPLEX = (r"AVERAGE", r"\bMEAN\b", r"MOYEN", r"MEDIAN", r"\bSOMME\b", r"\bSUM\b", r"RATIO", r"ECART", r"MARGE",
           r"MARGIN", r"DIFFEREN", r"COMPAR", r"VERSUS", r"\bVS\b", r"MORE THAN", r"LESS THAN", r"FEWER",
           r"PLUS DE \d", r"MOINS DE \d", r"AU MOINS", r"AT LEAST", r"BETWEEN", r"\bENTRE\b", r"\bWHERE\b",
           # "seats won by X" is simple; "turnout in constituencies won by X" is a filter
           r"(TURNOUT|PARTICIPATION|VOTANTS|INSCRITS).*(WON BY|GAGNEES? PAR|REMPORTEES? PAR)", r"LORSQUE", r"CORREL", r"SHARE OF",
           r"PROPORTION", r"POURCENTAGE DE", r"PER CANDIDATE", r"PAR CANDIDAT", r"MOST CANDIDATES",
           r"PLUS DE CANDIDATS", r"NUMBER OF CANDIDATES", r"NOMBRE DE CANDIDATS PAR", r"WOMEN", r"FEMMES",
           r"\bLISTS?\b", r"\bLISTES\b", r"UNOPPOSED", r"SEUL CANDIDAT", r"ONLY ONE", r"SECOND", r"DEUXIEME",
           r"RUNNER", r"\bNOT\b", r"\bSANS\b", r"\bEXCEPT", r"\bSAUF\b", r"CUMUL", r"TOTAL DES VOIX DE")


# ---- question analysis ----------------------------------------------------------------------------
@dataclass
class Q:
    text: str                   # normalized question
    lang: str
    circ: list[Match] = field(default_factory=list)
    regions: list[Match] = field(default_factory=list)
    parties: list[Match] = field(default_factory=list)
    candidates: list[Match] = field(default_factory=list)
    chart: str | None = None    # requested chart type

    def has(self, *patterns: str) -> bool:
        return any(re.search(p, self.text) for p in patterns)

    @property
    def circ_ids(self) -> list[str]:
        return sorted({c for m in self.circ for c in m.circ_ids})

    @property
    def region_values(self) -> list[str]:
        return [m.value for m in self.regions]

    @property
    def area(self) -> bool:
        return bool(self.circ or self.regions)

    @property
    def top_n(self) -> int:
        m = re.search(r"\b(\d{1,3})\b", self.text)
        if m:
            return max(1, min(int(m.group(1)), 100))
        single = (r"\bLA PLUS\b", r"\bLE PLUS\b", r"\bLE MEILLEUR\b", r"\bTHE (HIGHEST|LOWEST|BEST|MOST|LEAST)\b",
                  r"^(QUEL|QUELLE|WHICH|WHAT) ")
        return 1 if self.has(*single) and not self.has(r"\bLES\b", r"\bTOP\b") else 10

    @property
    def ascending(self) -> bool:
        return self.has(r"FAIBLE", r"BASSE", r"\bMOINS\b", r"PIRE", r"MINIMUM", r"\bBAS\b", r"LOWEST",
                        r"\bLEAST\b", r"WORST", r"SMALLEST")


def build_q(question: str, lang: str, matches: list[Match], chart: str | None) -> Q:
    q = Q(norm_key(question), lang, chart=chart)
    for m in matches:
        {"circonscription": q.circ, "region": q.regions, "party": q.parties,
         "candidate": q.candidates}[m.kind].append(m)
    # a name that is both a locality and a region (SAN PEDRO, YAMOUSSOUKRO): "région" decides
    if q.circ and q.regions:
        overlap = {r.mention for r in q.regions} & {c.mention for c in q.circ}
        if q.has(r"\bREGION", r"DISTRICT"):
            q.circ = [m for m in q.circ if m.mention not in overlap]
        else:
            q.regions = [m for m in q.regions if m.mention not in overlap]
    return q


# ---- plans ----------------------------------------------------------------------------------------
@dataclass
class Plan:
    intent: str
    family: str                       # aggregation | ranking | lookup
    sql: str
    say: Callable[[pd.DataFrame], str]
    chart_x: str | None = None
    chart_y: str | None = None


def _where_area(q: Q) -> str:
    return f"circ_id IN ({sq(q.circ_ids)})" if q.circ else f"region IN ({sq(q.region_values)})"


def _area_label(q: Q) -> str:
    return join_and([m.value for m in (q.circ or q.regions)], q.lang)


def i_candidate(q: Q) -> Plan | None:
    if not q.candidates:
        return None
    L = q.lang
    sql = ("SELECT row_id, candidate, party, circonscription, votes, vote_pct, rank_in_circ, is_elected, "
           f"source_page, excerpt FROM vw_results_clean WHERE candidate IN ({sq(m.value for m in q.candidates)}) "
           "ORDER BY votes DESC")

    def say(df):
        return "\n".join(
            f"- **{r.candidate}** ({r.party}), {r.circonscription} : {fmt_n(r.votes, L)} "
            f"{t(L, 'voix', 'votes')} ({fmt_pct(r.vote_pct, L)}), "
            + (t(L, "**élu(e)**", "**elected**") if r.is_elected
               else t(L, f"classé(e) {r.rank_in_circ}ᵉ", f"ranked #{r.rank_in_circ}")) + "."
            for r in df.itertuples())
    return Plan("candidate_lookup", "lookup", sql, say)


def i_count(q: Q) -> Plan | None:
    if not q.has(*COUNT):
        return None
    L = q.lang
    sql = ("SELECT COUNT(DISTINCT circ_id) AS circonscriptions, COUNT(*) AS candidatures, "
           "COUNT(DISTINCT party) AS partis, COUNT(DISTINCT region) AS regions FROM vw_results_clean")

    def say(df):
        r = df.iloc[0]
        return t(L,
                 f"**{fmt_n(r.circonscriptions, L)} circonscriptions**, **{fmt_n(r.candidatures, L)} candidatures** "
                 f"de **{fmt_n(r.partis, L)} partis ou groupements** (indépendants inclus), dans "
                 f"**{fmt_n(r.regions, L)} régions et districts**.",
                 f"**{fmt_n(r.circonscriptions, L)} constituencies**, **{fmt_n(r.candidatures, L)} candidacies** "
                 f"from **{fmt_n(r.partis, L)} parties or groups** (independents included), in "
                 f"**{fmt_n(r.regions, L)} regions and districts**.")
    return Plan("dataset_counts", "aggregation", sql, say)


def i_turnout_ranking(q: Q) -> Plan | None:
    breakdown = q.has(*BY_REGION, *BY_CIRC)
    if not (q.has(*TURNOUT) and (q.has(*RANK) or breakdown or (q.chart and not q.area))):
        return None
    L, order = q.lang, "ASC" if q.ascending else "DESC"
    by_region = q.has(r"REGION", r"DISTRICT") and not q.has(*BY_CIRC)
    limit = "" if (breakdown or q.chart) and not q.has(*RANK) else f" LIMIT {q.top_n}"
    if by_region:
        sql = ("SELECT region, taux_participation, inscrits, votants, nb_circonscriptions FROM vw_turnout_region "
               f"ORDER BY taux_participation {order}{limit}")
    else:
        sql = ("SELECT circonscription, region, taux_participation, inscrits, votants, source_page "
               f"FROM vw_turnout ORDER BY taux_participation {order}{limit}")
    what = t(L, "région", "region") if by_region else t(L, "circonscription", "constituency")

    def say(df):
        if len(df) == 1:
            r = df.iloc[0]
            sens = t(L, "la plus faible", "the lowest") if order == "ASC" else t(L, "la plus forte", "the highest")
            return t(L, f"La {what} avec {sens} participation est **{r.iloc[0]}** : ",
                     f"The {what} with {sens} turnout is **{r.iloc[0]}**: ") + (
                f"**{fmt_pct(r.taux_participation, L)}** ({fmt_n(r.votants, L)} "
                + t(L, "votants sur ", "voters out of ") + f"{fmt_n(r.inscrits, L)} " + t(L, "inscrits).", "registered)."))
        head = t(L, f"Taux de participation par {what} ({len(df)} lignes, ordre {'croissant' if order == 'ASC' else 'décroissant'}) :",
                 f"Turnout by {what} ({len(df)} rows, {'ascending' if order == 'ASC' else 'descending'}):")
        lines = [f"{i}. {r.iloc[0]} : {fmt_pct(r.taux_participation, L)}" for i, (_, r) in enumerate(df.head(15).iterrows(), 1)]
        more = t(L, f"\n… et {len(df) - 15} autres (voir le tableau).", f"\n… and {len(df) - 15} more (see table).") if len(df) > 15 else ""
        return head + "\n" + "\n".join(lines) + more
    x = "region" if by_region else "circonscription"
    return Plan("turnout_ranking", "ranking", sql, say, x, "taux_participation")


def i_turnout_place(q: Q) -> Plan | None:
    if not (q.has(*TURNOUT) and q.area):
        return None
    L = q.lang
    if q.circ:
        sql = ("SELECT circonscription, region, taux_participation, inscrits, votants, bulletins_nuls, "
               "bulletins_blancs, suffrages_exprimes, source_page FROM vw_turnout "
               f"WHERE circ_id IN ({sq(q.circ_ids)}) ORDER BY circ_id")
        label = "circonscription"
    else:
        sql = ("SELECT region, taux_participation, inscrits, votants, bulletins_nuls, bulletins_blancs, "
               f"nb_circonscriptions FROM vw_turnout_region WHERE region IN ({sq(q.region_values)})")
        label = "region"

    def say(df):
        return "\n".join(
            f"- **{getattr(r, label)}** : " + t(L, "participation de ", "turnout ")
            + f"**{fmt_pct(r.taux_participation, L)}** ({fmt_n(r.votants, L)} "
            + t(L, "votants sur ", "voters out of ") + f"{fmt_n(r.inscrits, L)} " + t(L, "inscrits).", "registered).")
            for r in df.itertuples())
    return Plan("turnout_area", "lookup", sql, say, label, "taux_participation")


def i_turnout_national(q: Q) -> Plan | None:
    if not q.has(*TURNOUT):
        return None
    L = q.lang
    sql = ("SELECT inscrits, votants, taux_participation, bulletins_nuls, bulletins_blancs, "
           "suffrages_exprimes, nb_bv, source_page FROM national_totals")

    def say(df):
        r = df.iloc[0]
        return t(L,
                 f"Au niveau national : **{fmt_n(r.inscrits, L)} inscrits**, **{fmt_n(r.votants, L)} votants**, "
                 f"soit une participation de **{fmt_pct(r.taux_participation, L)}** ; {fmt_n(r.suffrages_exprimes, L)} "
                 f"suffrages exprimés, {fmt_n(r.bulletins_nuls, L)} bulletins nuls et {fmt_n(r.bulletins_blancs, L)} "
                 f"blancs, dans {fmt_n(r.nb_bv, L)} bureaux de vote.",
                 f"Nationally: **{fmt_n(r.inscrits, L)} registered voters**, **{fmt_n(r.votants, L)} voters**, i.e. a "
                 f"turnout of **{fmt_pct(r.taux_participation, L)}**; {fmt_n(r.suffrages_exprimes, L)} valid votes, "
                 f"{fmt_n(r.bulletins_nuls, L)} invalid and {fmt_n(r.bulletins_blancs, L)} blank ballots, in "
                 f"{fmt_n(r.nb_bv, L)} polling stations.")
    return Plan("turnout_national", "aggregation", sql, say)


def i_party_in_area(q: Q) -> Plan | None:
    if not (q.parties and q.area):
        return None
    L, area = q.lang, _area_label(q)
    who = join_and([m.mention for m in q.parties], L)
    # aggregate without GROUP BY: always one row, so "0 seats" is answered explicitly
    sql = ("SELECT COUNT(*) AS nb_candidatures, COALESCE(SUM(CASE WHEN is_elected THEN 1 ELSE 0 END), 0) AS nb_elus, "
           "COALESCE(SUM(votes), 0) AS total_votes FROM vw_results_clean "
           f"WHERE {_where_area(q)} AND party_key IN ({sq(m.value for m in q.parties)})")

    def say(df):
        r = df.iloc[0]
        if r.nb_candidatures == 0:
            return t(L, f"**{who}** n'avait aucune candidature à {area} (0 élu).",
                     f"**{who}** had no candidate in {area} (0 seats).")
        return (f"**{who}** " + t(L, "à ", "in ") + f"{area} : **{plural(r.nb_elus, L, 'élu', 'seat')}** "
                + t(L, "pour ", "from ") + plural(r.nb_candidatures, L, "candidature", "candidacy")
                + f", {fmt_n(r.total_votes, L)} " + t(L, "voix.", "votes."))
    return Plan("party_in_area", "aggregation", sql, say)


def i_top_candidates(q: Q) -> Plan | None:
    if (not (q.has(*RANK) and (q.has(*RESULTS) or q.area or q.has(*WIN)))
            or q.has(*TURNOUT) or q.has(r"PARTY", r"PARTIES", r"\bPARTIS?\b")):
        return None
    L = q.lang
    by_pct = q.has(r"POURCENTAGE", r"PERCENT", r"%", r"\bPCT\b")
    winners_only = q.has(r"\bELUE?S?\b", r"ELECTED", r"WINNERS?", r"\bMPS?\b", r"DEPUTE")
    view = "vw_winners" if winners_only else "vw_results_clean"
    where = f" WHERE {_where_area(q)}" if q.area else ""
    order = "ASC" if q.ascending else "DESC"
    col = "vote_pct" if by_pct else "votes"
    sql = (f"SELECT row_id, candidate, party, circonscription, votes, vote_pct, source_page, excerpt FROM {view}"
           f"{where} ORDER BY {col} {order} LIMIT {q.top_n}")

    def say(df):
        scope = t(L, f" à {_area_label(q)}", f" in {_area_label(q)}") if q.area else ""
        head = t(L, f"Top {len(df)} par {'pourcentage' if by_pct else 'nombre de voix'}{scope} :",
                 f"Top {len(df)} by {'percentage' if by_pct else 'votes'}{scope}:")
        return head + "\n" + "\n".join(
            f"{i}. **{r.candidate}** ({r.party}, {r.circonscription}) : {fmt_n(r.votes, L)} "
            f"{t(L, 'voix', 'votes')}, {fmt_pct(r.vote_pct, L)}" for i, r in enumerate(df.itertuples(), 1))
    return Plan("top_candidates", "ranking", sql, say, "candidate", "vote_pct" if by_pct else "votes")


def i_party(q: Q) -> Plan | None:
    if not q.parties:
        return None
    L = q.lang
    sql = ("SELECT party, nb_elus, nb_candidatures, total_votes, vote_share_pct FROM vw_party_summary "
           f"WHERE party_key IN ({sq(m.value for m in q.parties)}) ORDER BY nb_elus DESC")

    def say(df):
        return "\n".join(
            f"- **{r.party}** : **{plural(r.nb_elus, L, 'siège', 'seat')}** " + t(L, "sur 205", "out of 205")
            + f", {plural(r.nb_candidatures, L, 'candidature', 'candidacy')}, "
            f"{fmt_n(r.total_votes, L)} " + t(L, "voix", "votes") + f" ({fmt_pct(r.vote_share_pct, L)} "
            + t(L, "des voix).", "of votes).") for r in df.itertuples())
    return Plan("party_seats", "aggregation", sql, say, "party", "nb_elus")


def i_winners_area(q: Q) -> Plan | None:
    if not (q.area and (q.has(*WIN) or not q.has(*RESULTS))):
        return None
    L = q.lang
    if q.circ:
        sql = ("SELECT row_id, circonscription, candidate, party, votes, vote_pct, source_page, excerpt "
               f"FROM vw_winners WHERE circ_id IN ({sq(q.circ_ids)}) ORDER BY circ_id")

        def say(df):
            return "\n".join(
                f"- **{r.circonscription}** : **{r.candidate}** ({r.party}) " + t(L, "élu(e) avec ", "elected with ")
                + f"{fmt_n(r.votes, L)} " + t(L, "voix", "votes") + f" ({fmt_pct(r.vote_pct, L)})."
                for r in df.itertuples())
        return Plan("winner_constituency", "lookup", sql, say, "circonscription", "votes")
    sql = ("SELECT region, party, COUNT(*) AS nb_elus FROM vw_winners "
           f"WHERE region IN ({sq(q.region_values)}) GROUP BY region, party ORDER BY region, nb_elus DESC")

    def say_region(df):
        out = []
        for region, g in df.groupby("region", sort=False):
            parts = [f"{r.party} {fmt_n(r.nb_elus, L)}" for r in g.itertuples()]
            out.append(f"- **{region}** ({plural(g.nb_elus.sum(), L, 'siège', 'seat')}) : {', '.join(parts)}.")
        return "\n".join(out)
    return Plan("winners_region", "aggregation", sql, say_region, "party", "nb_elus")


def i_results_area(q: Q) -> Plan | None:
    if not q.circ:
        return None
    L = q.lang
    sql = ("SELECT row_id, circonscription, candidate, party, votes, vote_pct, rank_in_circ, is_elected, "
           f"source_page, excerpt FROM vw_results_clean WHERE circ_id IN ({sq(q.circ_ids)}) "
           "ORDER BY circ_id, rank_in_circ")

    def say(df):
        out = []
        for circ, g in df.groupby("circonscription", sort=False):
            out.append(f"**{circ}** ({plural(len(g), L, 'candidature', 'candidacy')}) :")
            out += [f"{r.rank_in_circ}. {r.candidate} ({r.party}) : {fmt_n(r.votes, L)} {t(L, 'voix', 'votes')}, "
                    f"{fmt_pct(r.vote_pct, L)}" + (t(L, " — **élu(e)**", " — **elected**") if r.is_elected else "")
                    for r in g.head(10).itertuples()]
        return "\n".join(out)
    return Plan("results_constituency", "lookup", sql, say, "candidate", "votes")


def i_seats(q: Q) -> Plan | None:
    if not q.has(*WIN, *PARTY_WORDS):
        return None
    L = q.lang
    sql = ("SELECT party, nb_elus, nb_candidatures, total_votes, vote_share_pct FROM vw_party_summary "
           "WHERE nb_elus > 0 ORDER BY nb_elus DESC, total_votes DESC")

    def say(df):
        lines = [f"- **{r.party}** : {plural(r.nb_elus, L, 'siège', 'seat')} ({fmt_pct(r.vote_share_pct, L)} "
                 + t(L, "des voix)", "of votes)") for r in df.itertuples()]
        return t(L, "Répartition des 205 sièges par parti :", "Distribution of the 205 seats by party:") + "\n" + "\n".join(lines)
    return Plan("seats_by_party", "aggregation", sql, say, "party", "nb_elus")


# order matters: most specific first
INTENTS = [i_candidate, i_count, i_turnout_ranking, i_turnout_place, i_turnout_national, i_party_in_area,
           i_top_candidates, i_party, i_winners_area, i_results_area, i_seats]


def plan_for(q: Q) -> Plan | None:
    return next((p for intent in INTENTS if (p := intent(q))), None)
