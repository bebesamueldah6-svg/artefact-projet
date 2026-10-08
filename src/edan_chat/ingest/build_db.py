"""Build the structured dataset (Parquet + DuckDB) from the parsed PDF."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pandas as pd

from edan_chat.ingest.normalize import clean_label, looks_like_list, norm_key, party_key

SCHEMA_SQL = """
CREATE TABLE circonscriptions (
    circ_id              VARCHAR PRIMARY KEY,   -- '001'..'205' as printed in the PDF
    circonscription      VARCHAR NOT NULL,      -- label as printed (cleaned)
    circonscription_key  VARCHAR NOT NULL,      -- normalized matching key
    region               VARCHAR NOT NULL,      -- region / autonomous district
    region_key           VARCHAR NOT NULL,
    nb_bv                INTEGER,               -- number of polling stations
    inscrits             INTEGER,               -- registered voters
    votants              INTEGER,               -- voters
    taux_participation   DOUBLE,                -- % as printed (votants / inscrits)
    bulletins_nuls       INTEGER,
    suffrages_exprimes   INTEGER,
    bulletins_blancs     INTEGER,
    bulletins_blancs_pct DOUBLE,
    nb_candidatures      INTEGER,               -- number of candidates/lists on the ballot
    source_page          INTEGER,               -- PDF page where the row starts
    source_pages         VARCHAR                -- all pages spanned, e.g. '7,8'
);
CREATE TABLE candidatures (
    row_id          INTEGER PRIMARY KEY,        -- stable row id (document order)
    circ_id         VARCHAR NOT NULL REFERENCES circonscriptions(circ_id),
    party           VARCHAR NOT NULL,           -- party/group as printed ('INDEPENDANT' = no party)
    party_key       VARCHAR NOT NULL,           -- 'RHDP', 'PDCIRDA', ...
    candidate       VARCHAR NOT NULL,           -- candidate name or list name
    candidate_key   VARCHAR NOT NULL,
    is_list         BOOLEAN NOT NULL,           -- heuristic: named list vs person
    votes           INTEGER NOT NULL,           -- 'SCORES'
    vote_pct        DOUBLE NOT NULL,            -- '%' = votes / suffrages_exprimes
    rank_in_circ    INTEGER NOT NULL,           -- 1 = most votes in the circonscription
    is_elected      BOOLEAN NOT NULL,           -- 'ELU(E)' flag
    source_page     INTEGER NOT NULL,
    excerpt         VARCHAR NOT NULL            -- row re-rendered as text (provenance)
);
CREATE TABLE national_totals (
    nb_bv INTEGER, inscrits INTEGER, votants INTEGER, taux_participation DOUBLE,
    bulletins_nuls INTEGER, suffrages_exprimes INTEGER, bulletins_blancs INTEGER,
    bulletins_blancs_pct DOUBLE, total_votes_candidats INTEGER, source_page INTEGER
);
"""

# Curated semantic layer: the LLM is only allowed to query these views.
VIEWS_SQL = """
CREATE VIEW vw_results_clean AS
SELECT k.row_id, c.circ_id, c.circonscription, c.region, k.party, k.party_key,
       k.candidate, k.is_list, k.votes, k.vote_pct, k.rank_in_circ, k.is_elected,
       k.source_page
FROM candidatures k JOIN circonscriptions c USING (circ_id);

CREATE VIEW vw_winners AS
SELECT c.circ_id, c.circonscription, c.region, k.party, k.party_key, k.candidate,
       k.is_list, k.votes, k.vote_pct, k.source_page
FROM candidatures k JOIN circonscriptions c USING (circ_id)
WHERE k.is_elected;

CREATE VIEW vw_turnout AS
SELECT circ_id, circonscription, region, nb_bv, inscrits, votants, taux_participation,
       bulletins_nuls, suffrages_exprimes, bulletins_blancs, bulletins_blancs_pct, source_page
FROM circonscriptions;

CREATE VIEW vw_turnout_region AS
SELECT region,
       COUNT(*)                                   AS nb_circonscriptions,
       SUM(inscrits)                              AS inscrits,
       SUM(votants)                               AS votants,
       ROUND(100.0 * SUM(votants) / SUM(inscrits), 2) AS taux_participation,
       SUM(suffrages_exprimes)                    AS suffrages_exprimes,
       SUM(bulletins_nuls)                        AS bulletins_nuls,
       SUM(bulletins_blancs)                      AS bulletins_blancs
FROM circonscriptions GROUP BY region;

CREATE VIEW vw_party_summary AS
SELECT k.party, k.party_key,
       COUNT(*)                                     AS nb_candidatures,
       SUM(CASE WHEN k.is_elected THEN 1 ELSE 0 END) AS nb_elus,
       SUM(k.votes)                                 AS total_votes,
       ROUND(100.0 * SUM(k.votes) / (SELECT SUM(votes) FROM candidatures), 2) AS vote_share_pct
FROM candidatures k GROUP BY k.party, k.party_key;
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_frames(parsed: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    circ = pd.DataFrame(parsed["circonscriptions"])
    circ["circonscription"] = circ["circonscription"].map(clean_label)
    circ["region"] = circ.pop("region_raw").map(clean_label)
    circ["circonscription_key"] = circ["circonscription"].map(norm_key)
    circ["region_key"] = circ["region"].map(norm_key)

    k = pd.DataFrame(parsed["candidates"])
    k["party"] = k.pop("party_raw").map(clean_label)
    k["candidate"] = k.pop("candidate_raw").map(clean_label)
    k["party_key"] = k["party"].map(party_key)
    k["candidate_key"] = k["candidate"].map(norm_key)
    k["is_list"] = k["candidate"].map(looks_like_list)
    k["rank_in_circ"] = (
        k.groupby("circ_id")["votes"].rank(method="min", ascending=False).astype(int)
    )
    k["row_id"] = range(1, len(k) + 1)
    names = circ.set_index("circ_id")["circonscription"]
    k["excerpt"] = k.apply(
        lambda r: f"[p.{r.source_page}] {r.circ_id} {names[r.circ_id]} | {r.party} | "
                  f"{r.candidate} | {r.votes} voix | {r.vote_pct:.2f}%"
                  + (" | ELU(E)" if r.is_elected else ""),
        axis=1,
    )
    circ["nb_candidatures"] = circ["circ_id"].map(k.groupby("circ_id").size())

    n = parsed["national"]
    nat = pd.DataFrame([{
        "nb_bv": n["nb_bv"], "inscrits": n["inscrits"], "votants": n["votants"],
        "taux_participation": n["taux_participation"], "bulletins_nuls": n["bulletins_nuls"],
        "suffrages_exprimes": n["suffrages_exprimes"], "bulletins_blancs": n["bulletins_blancs"],
        "bulletins_blancs_pct": n["bulletins_blancs_pct"],
        "total_votes_candidats": n["total_scores"], "source_page": n["source_page"],
    }])
    return circ, k, nat


def build(parsed: dict, processed_dir: Path, db_path: Path) -> None:
    circ, cand, nat = to_frames(parsed)
    processed_dir.mkdir(parents=True, exist_ok=True)
    circ.to_parquet(processed_dir / "circonscriptions.parquet", index=False)
    cand.to_parquet(processed_dir / "candidatures.parquet", index=False)
    nat.to_parquet(processed_dir / "national_totals.parquet", index=False)
    circ.to_csv(processed_dir / "circonscriptions.csv", index=False, encoding="utf-8")
    cand.to_csv(processed_dir / "candidatures.csv", index=False, encoding="utf-8")

    tmp = db_path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(SCHEMA_SQL)
    for table, df in [("circonscriptions", circ), ("candidatures", cand), ("national_totals", nat)]:
        cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        con.register("df", df[cols])
        con.execute(f"INSERT INTO {table} SELECT * FROM df")
        con.unregister("df")
    con.execute(VIEWS_SQL)
    con.close()
    db_path.unlink(missing_ok=True)
    tmp.rename(db_path)


def write_manifest(path: Path, pdf_path: Path, report: dict) -> dict:
    manifest = {
        "pdf_file": pdf_path.name,
        "pdf_sha256": sha256(pdf_path),
        "dataset_version": sha256(pdf_path)[:12],
        "validation": report,
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
