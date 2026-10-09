from edan_chat import config


def make_agent():
    """Engine selected by config.ENGINE: 'rules' (default, no LLM) or 'llm' (Ollama)."""
    if config.ENGINE == "llm":
        from edan_chat.agent.pipeline import Agent
        return Agent()
    from edan_chat.agent.rules import RuleAgent
    return RuleAgent()
