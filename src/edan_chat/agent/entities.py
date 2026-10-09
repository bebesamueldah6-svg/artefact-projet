"""Entity resolution: map names typed by the user (typos, accents, casing, aliases) to database keys.

Question n-grams are fuzzy-matched against the localities, regions, candidates and parties of
the dataset. A locality can be *ambiguous*: it may belong to several circonscriptions
("BOUAKE" -> 060 ville / 061 sous-préfecture) or several localities may contain the mention
("BASSAM" -> GRAND-BASSAM / KETRO-BASSAM). Such matches carry `alternatives`, which the router
turns into a clarification question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import duckdb
from rapidfuzz import fuzz, process

from edan_chat import config
from edan_chat.ingest.normalize import norm_key, party_key, short_locality

# question words (FR + EN) that must never be matched as place / person names
_STOPWORDS = {
    "QUEL", "QUELS", "QUELLE", "QUELLES", "QUI", "QUE", "QUOI", "COMBIEN", "COMMENT", "OU",
    "EST", "SONT", "LES", "DES", "DANS", "POUR", "PAR", "AVEC", "SANS", "TAUX", "PARTI",
    "PARTIS", "VOIX", "VOTES", "SIEGES", "ELU", "ELUS", "ELUE", "GAGNE", "GAGNANT", "REGION",
    "COMMUNE", "VILLE", "CANDIDAT", "CANDIDATS", "PARTICIPATION", "RESULTAT", "RESULTATS",
    "TOP", "PLUS", "MOINS", "MONTRE", "MONTRER", "DONNE", "LISTE", "CIRCONSCRIPTION", "TOTAL",
    "NATIONAL", "SCORE", "SCORES", "A", "AU", "AUX", "LE", "LA", "DE", "DU", "ET", "EN", "UN", "UNE",
    "DISTRICT", "GRAPHIQUE", "HISTOGRAMME", "CAMEMBERT", "DIAGRAMME",
    # English
    "WHO", "WHAT", "WHICH", "HOW", "MANY", "MUCH", "THE", "IN", "OF", "BY", "FOR", "AND", "WON",
    "WIN", "WINNER", "WINNERS", "SEAT", "SEATS", "SHOW", "TURNOUT", "RATE", "PER", "PARTY",
    "PARTIES", "CANDIDATE", "CANDIDATES", "RESULT", "RESULTS", "CHART", "HISTOGRAM", "PIE", "BAR",
    "MOST", "HIGHEST", "LOWEST", "BEST", "IS", "ARE", "WAS", "DID", "ME", "GIVE", "LIST", "ELECTED",
    "CONSTITUENCY", "CONSTITUENCIES", "TELL", "ABOUT",
}

# long-form names / spellings -> party_key (normalized with party_key())
PARTY_ALIASES = {
    "RASSEMBLEMENTDESHOUPHOUETISTESPOURLADEMOCRATIEETLAPAIX": "RHDP",
    "HOUPHOUETISTES": "RHDP",
    "PARTIDEMOCRATIQUEDECOTEDIVOIRE": "PDCIRDA",
    "PDCI": "PDCIRDA",
    "FRONTPOPULAIREIVOIRIEN": "FPI",
    "INDEPENDANT": "INDEPENDANT", "INDEPENDANTS": "INDEPENDANT", "INDEPENDANTE": "INDEPENDANT",
    "INDEPENDENT": "INDEPENDANT", "INDEPENDENTS": "INDEPENDANT", "SANSPARTI": "INDEPENDANT",
}


@dataclass
class Match:
    kind: str           # 'circonscription' | 'region' | 'candidate' | 'party'
    mention: str        # text from the question (normalized)
    value: str          # canonical label in the database (locality key, region, candidate, party_key)
    score: float
    circ_ids: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)  # other readings: {value, circ_ids}

    @property
    def ambiguous(self) -> bool:
        return len(self.alternatives) > 1

    def hint(self) -> str:
        if self.kind == "circonscription":
            return f"'{self.mention}' = locality {self.value} -> circ_id IN ({', '.join(repr(c) for c in self.circ_ids)})"
        if self.kind == "region":
            return f"'{self.mention}' = region = '{self.value}'"
        if self.kind == "party":
            return f"'{self.mention}' = party_key = '{self.value}'"
        return f"'{self.mention}' = candidate = '{self.value}' (circ_id {', '.join(self.circ_ids)})"


@dataclass
class _Index:
    localities: dict[str, set[str]]              # locality key -> circ_ids
    regions: dict[str, str]                      # region key -> region label
    candidates: dict[str, tuple[str, set[str]]]  # candidate key -> (label, circ_ids)
    parties: dict[str, str]                      # party_key -> party label
    circ_labels: dict[str, str]                  # circ_id -> circonscription label


@lru_cache(maxsize=4)
def _index(db_path: str) -> _Index:
    con = duckdb.connect(db_path, read_only=True)
    try:
        circs = con.execute("SELECT circ_id, circonscription, region FROM circonscriptions").fetchall()
        cands = con.execute("SELECT candidate, candidate_key, circ_id FROM candidatures").fetchall()
        parties = con.execute("SELECT DISTINCT party, party_key FROM candidatures").fetchall()
    finally:
        con.close()
    localities: dict[str, set[str]] = {}
    regions: dict[str, str] = {}
    for circ_id, label, region in circs:
        for loc in short_locality(label):
            localities.setdefault(loc, set()).add(circ_id)
        key = re.sub(r"^DISTRICT AUTONOME D[E]? ", "", norm_key(region))
        regions[key] = region
    candidates: dict[str, tuple[str, set[str]]] = {}
    for label, key, circ_id in cands:
        candidates.setdefault(key, (label, set()))[1].add(circ_id)
    return _Index(localities, regions, candidates, {pk: p for p, pk in parties},
                  {c: label for c, label, _ in circs})


def circ_label(circ_id: str, db_path=config.DB_PATH) -> str:
    return _index(str(db_path)).circ_labels.get(circ_id, circ_id)


def _ngrams(tokens: list[str], n_max: int) -> list[tuple[int, int, str]]:
    out = []
    for n in range(n_max, 0, -1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                continue
            out.append((i, i + n, " ".join(gram)))
    return out


def _close(a: str, b: str) -> float:
    if a == b:
        return 100.0
    if min(len(a), len(b)) < 5:  # short names: exact only ("MAN", "BOUNA"...)
        return 0.0
    return fuzz.ratio(a, b)


def _contains(gram: str, loc: str) -> bool:
    """'BASSAM' is a whole-word part of 'GRAND BASSAM' / 'KETRO BASSAM'."""
    return len(gram) >= 4 and gram != loc and re.search(rf"\b{re.escape(gram)}\b", loc) is not None


def resolve(question: str, db_path=config.DB_PATH, threshold: float = 88.0) -> list[Match]:
    idx = _index(str(db_path))
    tokens = norm_key(question).split()
    used: set[int] = set()
    matches: list[Match] = []

    def take(i: int, j: int) -> bool:
        if used & set(range(i, j)):
            return False
        used.update(range(i, j))
        return True

    # 1. candidates (multi-word names, most specific)
    names = list(idx.candidates)
    for i, j, gram in _ngrams(tokens, 4):
        if j - i < 2:
            continue
        best = process.extractOne(gram, names, scorer=fuzz.token_sort_ratio, score_cutoff=90)
        if best and take(i, j):
            label, circ_ids = idx.candidates[best[0]]
            matches.append(Match("candidate", gram, label, best[1], sorted(circ_ids)))

    # 2. places: exact / fuzzy locality or region, or whole-word containment (ambiguity)
    for i, j, gram in _ngrams(tokens, 3):
        if used & set(range(i, j)):
            continue
        loc_scores = {loc: _close(gram, loc) for loc in idx.localities}
        best_loc = max(loc_scores, key=loc_scores.get)
        reg = max(idx.regions, key=lambda k: _close(gram, k))
        s_loc, s_reg = loc_scores[best_loc], _close(gram, reg)
        contained = [loc for loc in idx.localities if _contains(gram, loc)]
        if s_loc < threshold and s_reg < threshold and not contained:
            continue
        take(i, j)
        if s_loc >= threshold:
            circ_ids = sorted(idx.localities[best_loc])
            alts = ([{"value": best_loc, "circ_ids": [c]} for c in circ_ids] if len(circ_ids) > 1 else [])
            matches.append(Match("circonscription", gram, best_loc, s_loc, circ_ids, alts))
        elif contained:
            alts = [{"value": loc, "circ_ids": sorted(idx.localities[loc])} for loc in sorted(contained)]
            circ_ids = sorted({c for a in alts for c in a["circ_ids"]})
            matches.append(Match("circonscription", gram, contained[0], 90.0, circ_ids,
                                 alts if len(alts) > 1 else []))
        if s_reg >= threshold:
            matches.append(Match("region", gram, idx.regions[reg], s_reg))

    # 3. parties: acronyms with any punctuation ("R.H.D.P"), long-form names, aliases
    joined = party_key(question)
    words = [party_key(w) for w in re.split(r"[\s,;:?!()]+", question) if w]
    words += [party_key(" ".join(question.split()[k:k + 2])) for k in range(len(question.split()))]
    found: set[str] = set()
    for pk in idx.parties:
        if pk in words and pk != "INDEPENDANT":
            found.add(pk)
    for alias, pk in PARTY_ALIASES.items():
        if (alias in words or (len(alias) > 12 and alias in joined)) and pk in idx.parties:
            found.add(pk)
    matches += [Match("party", idx.parties[pk], pk, 100.0) for pk in sorted(found)]
    return matches
