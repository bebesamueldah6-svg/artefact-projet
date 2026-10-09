def main() -> None:
    """Terminal chat:  `uv run edan-chat`  (the web UI is `streamlit run src/edan_chat/app.py`)."""
    from edan_chat.agent import make_agent

    agent, history = make_agent(), []
    print("EDAN 2025 — posez une question (Ctrl+C pour quitter)")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not question:
            continue
        turn = agent.ask(question, history)
        print(turn.text)
        if turn.df is not None:
            print(f"\n{turn.df.head(20).to_string(index=False)}\n[SQL] {turn.sql}")
        history.append(turn)
