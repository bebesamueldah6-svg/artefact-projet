"""Retrieval path: BM25 over table rows rendered as text (row-as-text chunks), typo-tolerant.

Two chunk types, both carrying provenance (source_page + row id):
* candidature rows   -> the `excerpt` stored at ingestion ("[p.12] 064 DAKPADOU ... | RHDP | ...")
* circonscription rows -> turnout figures of the constituency

Query tokens unknown to the vocabulary are mapped to the closest known token (rapidfuzz) so
"Tiapum" still retrieves "TIAPOUM". The index is rebuilt in memory per dataset version.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import duckdb
from rapidfuzz import fuzz, process

from edan_chat import config
from edan_chat.agent.cache import dataset_version
from edan_chat.ingest.normalize import norm_key

_STOP = {"LE", "LA", "LES", "DE", "DU", "DES", "ET", "A", "AU", "AUX", "EN", "THE", "OF", "IN", "AND",
         "COMMUNE", "COMMUNES", "SOUS", "PREFECTURE", "PREFECTURES", "VILLE", "QUI", "WHO", "WHAT",
         "EST", "IS", "QUEL", "QUELLE", "TELL", "ME", "ABOUT", "PARLE", "MOI", "SHOW", "MONTRE",
         "INFO", "INFOS", "SUR", "ON", "CANDIDAT", "CANDIDATE", "FIND", "CHERCHE", "TROUVE"}


@dataclass
class Hit:
    doc_id: str          # 'cand:<row_id>' | 'circ:<circ_id>'
    kind: str            # 'candidature' | 'circonscription'
    source_page: int
    text: str
    score: float
    row_id: int | None = None
    circ_id: str | None = None


class BM25Index:
    k1, b = 1.5, 0.75

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.toks = [[t for t in norm_key(d["text"]).split() if t not in _STOP] for d in docs]
        self.avgdl = sum(map(len, self.toks)) / max(len(self.toks), 1)
        df = Counter(t for toks in self.toks for t in set(toks))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(toks) for toks in self.toks]
        self.vocab = list(self.idf)

    def _expand(self, token: str) -> list[tuple[str, float]]:
        if token in self.idf:
            return [(token, 1.0)]
        if len(token) < 4:
            return []
        best = process.extractOne(token, self.vocab, scorer=fuzz.ratio, score_cutoff=80)
        return [(best[0], best[1] / 100)] if best else []

    def search(self, query: str, k: int = config.RAG_TOP_K) -> list[Hit]:
        terms = [(t, w) for q in norm_key(query).split() if q not in _STOP for t, w in self._expand(q)]
        if not terms:
            return []
        scored = []
        for i, tf in enumerate(self.tf):
            dl = len(self.toks[i])
            s = sum(w * self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                    for t, w in terms if t in tf)
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [Hit(score=round(s, 3), **self.docs[i]) for s, i in scored[:k]]


@lru_cache(maxsize=2)
def get_index(db_path: str = str(config.DB_PATH), version: str = "") -> BM25Index:
    con = duckdb.connect(db_path, read_only=True)
    try:
        cand = con.execute("SELECT row_id, circ_id, source_page, excerpt FROM vw_results_clean").fetchall()
        circ = con.execute("""
            SELECT circ_id, source_page, '[p.' || source_page || '] ' || circ_id || ' ' || circonscription
                   || ' | région ' || region || ' | inscrits ' || inscrits || ' | votants ' || votants
                   || ' | participation ' || taux_participation || '% | exprimés ' || suffrages_exprimes
                   || ' | nuls ' || bulletins_nuls || ' | blancs ' || bulletins_blancs
            FROM vw_turnout""").fetchall()
    finally:
        con.close()
    docs = [{"doc_id": f"cand:{r}", "kind": "candidature", "row_id": r, "circ_id": c, "source_page": p,
             "text": e} for r, c, p, e in cand]
    docs += [{"doc_id": f"circ:{c}", "kind": "circonscription", "circ_id": c, "source_page": p, "text": t}
             for c, p, t in circ]
    return BM25Index(docs)


def search(query: str, k: int = config.RAG_TOP_K, db_path=config.DB_PATH) -> list[Hit]:
    return get_index(str(db_path), dataset_version()).search(query, k)
