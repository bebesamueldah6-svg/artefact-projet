"""Answer language: reply in the language of the question (French or English)."""

from __future__ import annotations

import re

_EN = {"the", "how", "many", "what", "which", "who", "did", "does", "is", "are", "was", "were", "in",
       "by", "of", "show", "me", "top", "win", "won", "seats", "turnout", "rate", "per", "chart",
       "histogram", "give", "list", "with", "most", "highest", "lowest", "candidates", "votes",
       "party", "parties", "region", "results", "winner", "winners", "and", "for", "ignore", "your",
       "to", "from", "all", "every", "table", "tables", "rows", "my", "then", "run", "return", "when",
       "day", "on", "election", "weather", "president", "generate", "database", "without", "about",
       "tell", "there", "any", "each", "where", "constituency", "constituencies", "pie", "bar", "it"}
_FR = {"le", "la", "les", "des", "du", "de", "combien", "quel", "quelle", "quels", "quelles", "qui",
       "a", "à", "est", "sont", "dans", "par", "pour", "taux", "sièges", "sieges", "gagné", "gagne",
       "élu", "elu", "élus", "résultats", "resultats", "montre", "donne", "graphique", "et", "au",
       "aux", "une", "un", "région", "parti", "partis", "voix", "participation", "en", "sur", "avec",
       "toutes", "tous", "tout", "moi", "ma", "mes", "tes", "ton", "liste", "donne-moi", "montre-moi",
       "circonscription", "circonscriptions", "élection", "election", "candidats", "pourcentage",
       "histogramme", "camembert", "où", "ou", "quoi", "comment", "ignore", "règles"}


def detect(text: str) -> str:
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", text.lower())
    en = sum(w in _EN for w in words)
    fr = sum(w in _FR for w in words)
    return "en" if en > fr else "fr"


def t(lang: str, fr: str, en: str) -> str:
    return en if lang == "en" else fr
