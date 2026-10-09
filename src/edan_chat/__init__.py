def main() -> None:
    """Terminal chat:  `uv run edan-chat`  (the web UI is `streamlit run src/edan_chat/app.py`)."""
    from edan_chat.agent import Agent, Session

    agent, session = Agent(), Session()
    print("EDAN 2025 — ask a question / posez une question (Ctrl+C to quit)")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not question:
            continue
        turn = agent.ask(question, session)
        print(turn.text)
        for i, opt in enumerate(turn.options, 1):
            print(f"  {i}. {opt['label']}")
        if turn.df is not None:
            print(f"\n{turn.df.drop(columns=['excerpt'], errors='ignore').head(20).to_string(index=False)}")
        print(f"[{turn.route} · {turn.intent} · {turn.latency_ms:.0f} ms · trace {turn.trace_id}]")
