"""Agent tests that run without Ollama (scripted fake LLM) against the real DuckDB file."""

import json

import pytest

from edan_chat import config
from edan_chat.agent.entities import resolve
from edan_chat.agent.pipeline import Agent
from edan_chat.agent.sql_guard import UnsafeSQLError, execute, validate

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(),
                                reason="run `python -m edan_chat.ingest` first")


class FakeLLM:
    """Returns scripted replies in order and records the prompts it received."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, json_mode=False):
        self.calls.append(messages)
        r = self.replies.pop(0)
        return json.dumps(r) if isinstance(r, dict) else r


# ---- SQL guard -------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM candidatures",
    "SELECT * FROM read_csv('C:/secret.csv')",
    "DROP VIEW vw_winners",
    "SELECT 1; SELECT 2",
    "SELECT getenv('PATH')",
    "WITH candidatures AS (SELECT * FROM candidatures) SELECT * FROM candidatures",
    "SELECT * FROM information_schema.tables",
    "ATTACH 'x.db'",
])
def test_guard_rejects(sql):
    with pytest.raises(UnsafeSQLError):
        validate(sql)


def test_guard_strips_llm_debris():
    # seen with qwen2.5:7b: a JSON brace leaking into the SQL string
    assert validate("SELECT * FROM vw_winners WHERE circ_id = '047'}").startswith(
        "SELECT * FROM vw_winners WHERE circ_id = '047'")
    assert validate("```sql\nSELECT * FROM vw_winners;\n```").startswith("SELECT * FROM vw_winners")


def test_guard_caps_rows():
    assert validate("SELECT * FROM vw_results_clean").endswith(f"LIMIT {config.SQL_MAX_ROWS + 1}")
    assert validate("SELECT * FROM vw_winners LIMIT 5").endswith("LIMIT 5")
    r = execute("SELECT * FROM vw_results_clean")
    assert len(r.df) == config.SQL_MAX_ROWS and r.truncated


# ---- entities ----------------------------------------------------------------------------------
@pytest.mark.parametrize("question, kind, value", [
    ("Qui a gagné à yopougon ?", "circonscription", "YOPOUGON"),
    ("participation à Agbovile", "circonscription", "AGBOVILLE"),   # typo
    ("score de koffi aka charles", "candidate", "KOFFI AKA CHARLES"),
    ("sièges du PDCI", "party", "PDCIRDA"),
    ("région du Poro", "region", "PORO"),
])
def test_resolve(question, kind, value):
    assert any(m.kind == kind and m.value == value for m in resolve(question))


def test_resolve_ignores_generic_questions():
    assert resolve("Quel parti a le plus de sièges ?") == []


# ---- pipeline ----------------------------------------------------------------------------------
def test_answer_flow(tmp_path):
    llm = FakeLLM(
        {"action": "sql", "sql": "SELECT circonscription, candidate, party, votes, source_page "
                                 "FROM vw_winners WHERE circ_id = '047'"},
        "Le siège de Yopougon est remporté par ...",
    )
    turn = Agent(llm, trace_dir=tmp_path).ask("Qui a gagné à Yopougon ?")
    assert turn.kind == "answer" and len(turn.df) == 1
    assert turn.source_pages and "circ_id IN ('047')" in llm.calls[0][-1]["content"]
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


def test_repairs_bad_sql(tmp_path):
    llm = FakeLLM(
        {"action": "sql", "sql": "SELECT * FROM candidatures"},
        {"action": "sql", "sql": "SELECT party, nb_elus FROM vw_party_summary ORDER BY nb_elus DESC LIMIT 3"},
        "RHDP en tête.",
    )
    turn = Agent(llm, trace_dir=tmp_path).ask("Sièges par parti ?")
    assert turn.kind == "answer"
    assert [a["error"] is None for a in turn.attempts] == [False, True]
    assert turn.df.iloc[0]["party"] == "RHDP"


def test_retries_empty_result_with_hints(tmp_path):
    llm = FakeLLM(
        # qwen2.5:7b invented region = 'BOUAKE' (no such region) despite the circ_id hint
        {"action": "sql", "sql": "SELECT circonscription, taux_participation FROM vw_turnout "
                                 "WHERE region = 'BOUAKE'"},
        {"action": "sql", "sql": "SELECT circonscription, taux_participation FROM vw_turnout "
                                 "WHERE circ_id IN ('060', '061')"},
        "Participation à Bouaké ...",
    )
    turn = Agent(llm, trace_dir=tmp_path).ask("Taux de participation à Bouaké ?")
    assert [a["error"] for a in turn.attempts] == ["0 ligne", None]
    assert len(turn.df) == 2 and "circ_id IN ('060', '061')" in llm.calls[1][-1]["content"]


def test_gives_up_after_repairs(tmp_path):
    bad = {"action": "sql", "sql": "DELETE FROM vw_winners"}
    turn = Agent(FakeLLM(bad, bad), trace_dir=tmp_path).ask("Efface tout")
    assert turn.kind == "error" and turn.df is None


def test_refusal_passthrough(tmp_path):
    llm = FakeLLM({"action": "refuse", "message": "Hors périmètre."})
    turn = Agent(llm, trace_dir=tmp_path).ask("Quelle est la capitale du Ghana ?")
    assert (turn.kind, turn.text) == ("refuse", "Hors périmètre.")
