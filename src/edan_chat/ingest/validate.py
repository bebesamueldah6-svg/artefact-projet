"""Arithmetic consistency checks proving the extraction is complete and correctly aligned."""

from __future__ import annotations

import duckdb

CHECKS: dict[str, str] = {
    # each query must return 0 rows to pass
    "circ_count_is_205": "SELECT 1 WHERE (SELECT COUNT(*) FROM circonscriptions) <> 205",
    "votants_eq_nuls_plus_exprimes":
        "SELECT circ_id FROM circonscriptions WHERE votants <> bulletins_nuls + suffrages_exprimes",
    "sum_votes_eq_exprimes_minus_blancs": """
        SELECT c.circ_id FROM circonscriptions c JOIN candidatures k USING (circ_id)
        GROUP BY c.circ_id, c.suffrages_exprimes, c.bulletins_blancs
        HAVING SUM(k.votes) <> c.suffrages_exprimes - c.bulletins_blancs""",
    "vote_pct_recomputable": """
        SELECT k.row_id FROM candidatures k JOIN circonscriptions c USING (circ_id)
        WHERE ABS(100.0 * k.votes / c.suffrages_exprimes - k.vote_pct) > 0.02""",
    "taux_participation_recomputable": """
        SELECT circ_id FROM circonscriptions
        WHERE ABS(100.0 * votants / inscrits - taux_participation) > 0.02""",
    "exactly_one_elected_per_circ": """
        SELECT circ_id FROM candidatures GROUP BY circ_id
        HAVING SUM(CASE WHEN is_elected THEN 1 ELSE 0 END) <> 1""",
    "elected_is_top_ranked": "SELECT row_id FROM candidatures WHERE is_elected AND rank_in_circ <> 1",
    "national_totals_match": """
        SELECT 1 FROM national_totals n,
          (SELECT SUM(inscrits) i, SUM(votants) v, SUM(suffrages_exprimes) e,
                  SUM(bulletins_nuls) nu, SUM(bulletins_blancs) b, SUM(nb_bv) bv
           FROM circonscriptions) s
        WHERE n.inscrits <> s.i OR n.votants <> s.v OR n.suffrages_exprimes <> s.e
           OR n.bulletins_nuls <> s.nu OR n.bulletins_blancs <> s.b OR n.nb_bv <> s.bv
           OR n.total_votes_candidats <> (SELECT SUM(votes) FROM candidatures)""",
    "no_empty_names":
        "SELECT row_id FROM candidatures WHERE candidate = '' OR party = ''",
}


def run_checks(db_path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    report = {}
    for name, sql in CHECKS.items():
        bad = [r[0] for r in con.execute(sql).fetchall()]
        report[name] = {"passed": not bad, "failures": bad[:20]}
    con.close()
    return report
