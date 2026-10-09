"""Entity normalization rules (documented in README §Normalization).

* `norm_key(s)`  – matching key: Unicode NFKD, accents stripped, upper-case,
  apostrophes/punctuation -> space, whitespace collapsed.
  "Côte d’Ivoire" -> "COTE D IVOIRE", "R.H.D.P" -> "R H D P".
* `party_key(s)` – party key: like norm_key but all separators removed,
  so "R.H.D.P", "rhdp", "R H D P" -> "RHDP".
* `clean_label(s)` – display cleanup of PDF text: fixes line-break hyphenation
  ("SOUS- PREFECTURES" -> "SOUS-PREFECTURES"), spaces after commas, multiple spaces.
  Original accents/casing of the PDF are kept for display.
"""

from __future__ import annotations

import re
import unicodedata

_LIST_WORDS = {
    "POUR", "UNE", "UN", "LA", "LE", "LES", "DES", "DU", "EN", "NOTRE", "NOS", "ENSEMBLE",
    "AVEC", "COTE", "IVOIRE", "DIVOIRE", "ALLIANCE", "PAIX", "SOLIDAIRE", "PROSPERE",
    "AVENIR", "UNION", "RASSEMBLEMENT", "MOUVEMENT", "GENERATION", "GENERATIONS",
    "DEVELOPPEMENT", "PARLEMENT", "VOIX", "ESPOIR", "LESPOIR", "JEUNESSE", "PASSION",
    "CEST", "REALISONS", "EGALITE", "TOUS", "ACTION", "AU", "AUX", "ET",
}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_key(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def party_key(s: str | None) -> str:
    return norm_key(s).replace(" ", "")


def clean_label(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("·", "-")  # PDF uses a middle dot as hyphen in a few labels
    s = re.sub(r"(\w)- (\w)", r"\1-\2", s)  # hyphenation at line breaks
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",(\S)", r", \1", s)
    return re.sub(r"\s+", " ", s).strip()


def looks_like_list(name: str) -> bool:
    """Heuristic: the entry is a named list (multi-seat ballot) rather than a person."""
    tokens = set(norm_key(name).split())
    return len(tokens & _LIST_WORDS) >= 1 and len(tokens) >= 3


def short_locality(circonscription: str) -> list[str]:
    """Locality names mentioned in a circonscription label (used for entity resolution).

    "GOMON ET SIKENSI, COMMUNES ET SOUS-PREFECTURES" -> ["GOMON", "SIKENSI"]
    """
    parts = []
    for chunk in circonscription.split(","):  # split before norm_key, which drops commas
        k = norm_key(chunk)
        k = re.sub(r"\b(COMMUNES?|SOUS PREFECTURES?|PREFECTURES?|VILLE|ET|DE|DU|DES|LA|LE)\b", ",", k)
        parts += [p.strip() for p in k.split(",")]
    return list(dict.fromkeys(p for p in parts if len(p) >= 3))
