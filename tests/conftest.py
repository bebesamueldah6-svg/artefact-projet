import json

import pytest

from edan_chat import config
from edan_chat.agent import Agent
from edan_chat.agent.llm import ChatModel

if not config.DB_PATH.exists():
    pytest.skip("run `python -m edan_chat.ingest` first", allow_module_level=True)


class FakeLLM(ChatModel):
    """Scripted replies (dict -> JSON); records the prompts it received."""

    model = "fake"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def available(self) -> bool:
        return True

    def chat(self, messages, json_mode=False):
        self.calls.append(messages)
        r = self.replies.pop(0)
        return (json.dumps(r) if isinstance(r, dict) else r), {"prompt_tokens": 100, "completion_tokens": 10}


@pytest.fixture
def fake_llm():
    return FakeLLM


@pytest.fixture
def rules_agent(tmp_path):
    """Agent without LLM (deterministic paths only), traces in a temp dir."""
    return Agent(llm=ChatModel(), trace_dir=tmp_path)
