"""The rule engine is deterministic, so the whole evaluation set runs as unit tests."""

import pytest

from edan_chat import config
from edan_chat.agent.rules import RuleAgent
from edan_chat.eval import CASES, _norm

pytestmark = pytest.mark.skipif(not config.DB_PATH.exists(),
                                reason="run `python -m edan_chat.ingest` first")


def test_eval_cases(tmp_path):
    agent, history = RuleAgent(trace_dir=tmp_path), []
    for case in CASES:
        turn = agent.ask(case["q"], history if case.get("follow_up") else [])
        history.append(turn)
        kinds = case["kind"] if isinstance(case["kind"], tuple) else (case["kind"],)
        assert turn.kind in kinds, (case["q"], turn.text)
        for expected in case["expect"]:
            assert _norm(expected) in _norm(turn.text), (case["q"], expected, turn.text)


def test_zero_seats_is_explicit(tmp_path):
    turn = RuleAgent(trace_dir=tmp_path).ask("Combien de sièges pour le PDCI dans le Poro ?")
    assert "0 élu" in turn.text or "aucune candidature" in turn.text


def test_unknown_question_gets_help(tmp_path):
    turn = RuleAgent(trace_dir=tmp_path).ask("Bonjour, ça va ?")
    assert turn.kind == "clarify" and "Yopougon" in turn.text
