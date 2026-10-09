"""The deterministic part of the offline evaluation runs as a regression test (CI)."""

from edan_chat.eval import run


def test_deterministic_eval_suite_passes():
    results, summary = run(use_llm=False, quiet=True)
    failures = [(r["id"], r["problems"], r["answer"][:120]) for r in results if not r["ok"]]
    assert not failures, failures
    assert summary["grounding"]["grounded"] == summary["grounding"]["answers"]
