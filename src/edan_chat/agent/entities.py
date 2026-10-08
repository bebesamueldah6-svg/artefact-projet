"""Entity resolution: map names typed by the user (with typos / missing accents) to database keys.

The LLM cannot guess that "Yop" or "yopougon" is circonscription '047', nor how a candidate's
name is spelled in the PDF. We fuzzy-match question n-grams against the known localities,
regions, candidates and parties and pass the hits to the SQL generator as hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import duckdb
from rapidfuzz import fuzz

from edan_chat import config
from edan_chat.ingest.normalize import norm_key, party_key, short_locality

# frequent question words that must never be matched as place / person names
_STOPWORDS = {
    "QUEL", "QUELS", "QUELLE", "QUELLES", "QUI", "QUE", "QUOI", "COMBIEN", "COMMENT", "OU",
    "EST", "SONT", "LES", "DES", "DANS", "POUR", "PAR", "AVEC", "SANS", "TAUX", "PARTI",
    "PARTIS", "VOIX", "VOTES", "SIEGES", "ELU", "ELUS", "ELUE", "GAGNE", "GAGNANT", "REGION",
    "COMMUNE", "VILLE", "CANDIDAT", "CANDIDATS", "PARTICIPATION", "RESULTAT", "RESULTATS",
    "TOP", "PLUS", "MOINS", "MONTRE", "MONTRER", "DONNE", "LISTE", "CIRCONSCRIPTION", "TOTAL",
    "NATIONAL", "SCORE", "SCORES", "A", "AU", "AUX", "LE", "LA", "DE", "DU", "ET", "EN", "UN", "UNE",
}


@dataclass
class Match:
    kind: str           # 'circonscription' | 'region' | 'candidate' | 'party'
    mention: str        # text from the question
    value: str          # canonical label in the database
    score: float
    circ_ids: list[str] = field(default_factory=list)

    def hint(self) -> str:
        if self.kind == "circonscription":
            return (f"« {self.mention} » = localité {self.value} -> circ_id IN "
                    f"({', '.join(repr(c) for c in self.circ_ids)})")
        if self.kind == "region":
            return f"« {self.mention} » = region = '{self.value}'"
        if self.kind == "party":
            return f"« {self.mention} » = party_key = '{self.value}'"
        return (f"« {self.mention} » = candidate = '{self.value}' "
                f"(circ_id {', '.join(self.circ_ids)})")


@dataclass
class _Index:
    localities: dict[str, set[str]]       # locality key -> circ_ids
    regions: dict[str, str]               # region key -> region label
    candidates: dict[str, tuple[str, set[str]]]  # candidate key -> (label, circ_ids)
    parties: dict[str, str]               # party_key -> party label


@lru_cache(maxsize=4)
def _index(db_path: str) -> _Index:
    con = duckdb.connect(db_path, read_only=True)
    circs = con.execute("SELECT circ_id, circonscription, region FROM circonscriptions").fetchall()
    cands = con.execute("SELECT candidate, candidate_key, circ_id FROM candidatures").fetchall()
    parties = con.execute("SELECT DISTINCT party, party_key FROM candidatures").fetchall()
    con.close()

    localities: dict[str, set[str]] = {}
    regions: dict[str, str] = {}
    for circ_id, label, region in circs:
        for loc in short_locality(label):
            localities.setdefault(loc, set()).add(circ_id)
        regions[norm_key(region).replace("DISTRICT AUTONOME D ", "").replace("DISTRICT AUTONOME DE ", "")] = region
    candidates: dict[str, tuple[str, set[str]]] = {}
    for label, key, circ_id in cands:
        candidates.setdefault(key, (label, set()))[1].add(circ_id)
    return _Index(localities, regions, candidates, {pk: p for p, pk in parties})


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

    # candidates first (longest, most specific), then places, then parties
    for i, j, gram in _ngrams(tokens, 4):
        if j - i < 2:
            continue
        best = max(idx.candidates, key=lambda k: fuzz.token_sort_ratio(gram, k))
        score = fuzz.token_sort_ratio(gram, best)
        if score >= 90 and take(i, j):
            label, circ_ids = idx.candidates[best]
            matches.append(Match("candidate", gram, label, score, sorted(circ_ids)))

    for i, j, gram in _ngrams(tokens, 3):
        reg = max(idx.regions, key=lambda k: _close(gram, k))
        loc = max(idx.localities, key=lambda k: _close(gram, k))
        s_reg, s_loc = _close(gram, reg), _close(gram, loc)
        if max(s_reg, s_loc) < threshold or not take(i, j):
            continue
        if s_loc >= threshold:
            matches.append(Match("circonscription", gram, loc, s_loc, sorted(idx.localities[loc])))
        if s_reg >= threshold:
            matches.append(Match("region", gram, idx.regions[reg], s_reg))

    pk_tokens = [party_key(t) for t in question.replace("-", " ").split()]
    joined = party_key(question)
    for pk, label in idx.parties.items():
        if pk == "INDEPENDANT":
            hit = "INDEPENDANT" in joined  # also matches the plural
        else:
            hit = pk in pk_tokens or (pk == "PDCIRDA" and "PDCI" in pk_tokens)
        if hit:
            matches.append(Match("party", label, pk, 100.0))
    return matches


def hints(matches: list[Match]) -> str:
    return "\n".join(f"- {m.hint()}" for m in matches)
