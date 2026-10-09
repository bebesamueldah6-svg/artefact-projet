"""Input guardrails: adversarial requests and out-of-scope questions (checked before any routing).

This is a first line of defence only. Even if a malicious request slipped through, the SQL
guard (SELECT-only, allowlists, LIMIT, read-only DB) and the absence of any code execution keep
the data safe; secrets are never placed in prompts, so they cannot be leaked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from edan_chat.agent.lang import t
from edan_chat.ingest.normalize import norm_key


@dataclass
class Verdict:
    category: str          # destructive | prompt_injection | exfiltration | out_of_scope
    message: str
    safe_alternative: str | None = None   # name of a safe action the router can run instead


_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("destructive", (r"\bDROP\b", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bINSERT\b", r"\bUPDATE\b",
                     r"\bALTER\b", r"CREATE (TABLE|VIEW|INDEX)", r"\bATTACH\b", r"\bCOPY\b",
                     r"\bGRANT\b", r"SUPPRIM", r"EFFAC", r"MODIFIE (LA|LES|UNE|DES) (BASE|DONNEES|TABLE)",
                     r"CHANGE THE (DATA|DATABASE|TABLE)")),
    ("exfiltration", (r"EXFILTR", r"ENTIRE DATABASE", r"WHOLE DATABASE", r"FULL DATABASE",
                      r"EVERY TABLE", r"ALL TABLES", r"ALL ROWS", r"WITHOUT (A )?LIMIT", r"NO LIMIT",
                      r"TOUTE LA BASE", r"TOUTES LES TABLES", r"TOUTES LES LIGNES", r"SANS LIMITE",
                      r"\bDUMP\b", r"BASE ENTIERE", r"BASE COMPLETE")),
    ("prompt_injection", (r"IGNORE (YOUR|ALL|THE|PREVIOUS|ANY)", r"IGNORE (TES|VOS|LES) (REGLES|INSTRUCTIONS)",
                          r"OUBLIE (TES|VOS|LES)", r"SYSTEM PROMPT", r"PROMPT SYSTEME", r"API KEYS?",
                          r"CLES? API", r"PASSWORD", r"MOT DE PASSE", r"\bSECRETS?\b", r"\bTOKENS?\b",
                          r"\bENV\b", r"JAILBREAK", r"DEVELOPER MODE", r"TES INSTRUCTIONS",
                          r"YOUR INSTRUCTIONS", r"\bEXEC", r"\bPYTHON\b", r"SHELL", r"\bEVAL\b")),
]

# topics the PDF cannot answer (other elections, people's roles, weather, predictions...)
_OUT_OF_SCOPE = (r"METEO", r"WEATHER", r"\bPRESIDENT", r"PRESIDENTIEL", r"MUNICIPAL", r"SENATORIAL",
                 r"REFERENDUM", r"PREMIER MINISTRE", r"PRIME MINISTER", r"\bMINISTR", r"\bMINISTER",
                 r"PREDI", r"PREVOI", r"PREVISION", r"FORECAST", r"SONDAGE", r"\bPOLL\b",
                 r"VA GAGNER", r"WILL WIN", r"POURQUOI", r"\bWHY\b", r"\bAGE\b", r"BIOGRAPH",
                 r"POPULATION", r"\bPIB\b", r"\bGDP\b", r"CAPITALE", r"CAPITAL CITY", r"RECETTE",
                 r"RECIPE", r"FOOTBALL", r"\b(EN|IN|OF|FOR) (19\d\d|20(?!25\b)\d\d)\b")


def check(question: str, lang: str) -> Verdict | None:
    q = norm_key(question)
    for category, patterns in _RULES:
        if any(re.search(p, q) for p in patterns):
            return _verdict(category, lang)
    if any(re.search(p, q) for p in _OUT_OF_SCOPE):
        return Verdict("out_of_scope", "")
    return None


def _verdict(category: str, lang: str) -> Verdict:
    if category == "destructive":
        return Verdict(category, t(
            lang,
            "Je refuse : les données sont en **lecture seule**. Aucune opération de modification "
            "(DROP, DELETE, UPDATE, INSERT…) n'est exécutée — seules des requêtes SELECT validées le sont.",
            "I can't do that: the data is **read-only**. No modifying operation (DROP, DELETE, UPDATE, "
            "INSERT…) is ever executed — only validated SELECT queries are."))
    if category == "prompt_injection":
        return Verdict(category, t(
            lang,
            "Je refuse : je ne divulgue ni mes instructions internes ni de secrets (clés API, "
            "configuration), et je ne contourne pas mes règles. Je peux en revanche répondre à toute "
            "question sur les résultats des législatives 2025.",
            "I can't do that: I don't reveal internal instructions or secrets (API keys, configuration) "
            "and I don't bypass my rules. I can answer any question about the 2025 legislative results."),
            "dataset_overview")
    return Verdict(category, t(
        lang,
        "Je refuse d'extraire la base complète : chaque résultat est **plafonné** (LIMIT) et seules les "
        "vues documentées sont accessibles. Voici à la place un aperçu de ce que contient le jeu de données :",
        "I won't dump the whole database: every result is **capped** (LIMIT) and only the documented "
        "views are accessible. Here is an overview of what the dataset contains instead:"),
        "dataset_overview")


OVERVIEW_SQL = """
SELECT 'vw_results_clean' AS vue, 'une ligne par candidature / one row per candidacy' AS contenu,
       (SELECT COUNT(*) FROM vw_results_clean) AS lignes
UNION ALL SELECT 'vw_winners', 'élus / winners', (SELECT COUNT(*) FROM vw_winners)
UNION ALL SELECT 'vw_turnout', 'participation par circonscription / turnout per constituency',
       (SELECT COUNT(*) FROM vw_turnout)
UNION ALL SELECT 'vw_turnout_region', 'participation par région / turnout per region',
       (SELECT COUNT(*) FROM vw_turnout_region)
UNION ALL SELECT 'vw_party_summary', 'bilan par parti / per-party summary', (SELECT COUNT(*) FROM vw_party_summary)
UNION ALL SELECT 'national_totals', 'totaux nationaux / national totals', (SELECT COUNT(*) FROM national_totals)
"""
