import pytest

from edan_chat import config
from edan_chat.agent.sql_guard import UnsafeSQLError, execute, validate


@pytest.mark.parametrize("sql", [
    "SELECT * FROM candidatures",                                   # base table, not a curated view
    "SELECT * FROM read_csv('C:/secret.csv')",                      # table function
    "DROP VIEW vw_winners",
    "DELETE FROM vw_winners",
    "UPDATE vw_winners SET votes = 0",
    "SELECT 1; SELECT 2",                                           # stacked queries
    "SELECT getenv('PATH')",
    "WITH candidatures AS (SELECT * FROM candidatures) SELECT * FROM candidatures",  # CTE shadowing
    "SELECT * FROM information_schema.tables",
    "ATTACH 'x.db'",
    "SELECT password FROM vw_winners",                              # column allowlist
])
def test_guard_rejects(sql):
    with pytest.raises(UnsafeSQLError):
        validate(sql)


def test_guard_allows_aliases_and_ctes():
    validate("WITH t AS (SELECT party, COUNT(*) AS n FROM vw_winners GROUP BY party) SELECT party, n FROM t")
    validate("SELECT w.party FROM vw_winners AS w")


def test_guard_strips_llm_debris():
    assert validate("SELECT * FROM vw_winners WHERE circ_id = '047'}").startswith(
        "SELECT * FROM vw_winners WHERE circ_id = '047'")
    assert validate("```sql\nSELECT * FROM vw_winners;\n```").startswith("SELECT * FROM vw_winners")


def test_guard_caps_rows():
    assert validate("SELECT * FROM vw_results_clean").endswith(f"LIMIT {config.SQL_MAX_ROWS + 1}")
    assert validate("SELECT * FROM vw_winners LIMIT 5").endswith("LIMIT 5")
    r = execute("SELECT * FROM vw_results_clean")
    assert len(r.df) == config.SQL_MAX_ROWS and r.truncated
