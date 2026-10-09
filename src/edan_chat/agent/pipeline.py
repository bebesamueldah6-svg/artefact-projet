"""LLM engine (ENGINE=llm). Question -> entity hints -> SQL (LLM) -> guard + execute (with one repair round) -> answer (LLM).

Every turn is traced as one JSON line in TRACE_DIR for auditability.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import duckdb
import pandas as pd

from edan_chat import config
from edan_chat.agent import entities, prompts
from edan_chat.agent.llm import ChatModel, LLMError, OllamaChat, parse_json
from edan_chat.agent.sql_guard import QueryResult, UnsafeSQLError, execute

MAX_REPAIRS = 1
ANSWER_ROWS = 30  # rows shown to the answer-writing LLM


@dataclass
class Turn:
    question: str
    kind: str = "answer"                # 'answer' | 'clarify' | 'refuse' | 'error'
    text: str = ""
    sql: str | None = None
    df: pd.DataFrame | None = None
    truncated: bool = False
    hints: list[str] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)  # SQL tries and their errors
    elapsed_s: float = 0.0


class Agent:
    def __init__(self, llm: ChatModel | None = None, db_path=config.DB_PATH,
                 trace_dir=config.TRACE_DIR):
        self.llm = llm or OllamaChat()
        self.db_path = db_path
        self.trace_dir = trace_dir

    # ---- public -------------------------------------------------------------------------
    def ask(self, question: str, history: list[Turn] | None = None) -> Turn:
        t0 = time.perf_counter()
        turn = Turn(question=question.strip())
        try:
            self._run(turn, history or [])
        except LLMError as e:
            turn.kind, turn.text = "error", str(e)
        turn.elapsed_s = round(time.perf_counter() - t0, 2)
        write_trace(turn, self.trace_dir, getattr(self.llm, "model", type(self.llm).__name__))
        return turn

    # ---- steps --------------------------------------------------------------------------
    def _run(self, turn: Turn, history: list[Turn]) -> None:
        matches = entities.resolve(turn.question, self.db_path)
        turn.hints = [m.hint() for m in matches]
        messages = [{"role": "system", "content": prompts.SQL_SYSTEM}]
        for past in history[-3:]:  # short memory for follow-up questions
            if past.kind == "answer" and past.sql:
                messages += [
                    {"role": "user", "content": past.question},
                    {"role": "assistant", "content": json.dumps({"action": "sql", "sql": past.sql},
                                                                ensure_ascii=False)},
                ]
        user = turn.question
        if turn.hints:
            user += "\nIndices :\n" + "\n".join(f"- {h}" for h in turn.hints)
        messages.append({"role": "user", "content": user})

        result: QueryResult | None = None
        for attempt in range(MAX_REPAIRS + 1):
            plan = parse_json(self.llm.chat(messages, json_mode=True))
            action = plan.get("action")
            if action in ("clarify", "refuse"):
                turn.kind, turn.text = action, plan.get("message") or "Pouvez-vous préciser ?"
                return
            sql = plan.get("sql") or ""
            try:
                result = execute(sql, self.db_path)
            except (UnsafeSQLError, duckdb.Error) as e:  # error is fed back for repair
                feedback = f"Cette requête a échoué : {e}\nCorrige-la."
                turn.attempts.append({"sql": sql, "error": str(e)})
            else:
                # an empty result with known entities usually means an invented filter
                if not result.df.empty or not turn.hints or attempt == MAX_REPAIRS:
                    turn.attempts.append({"sql": sql, "error": None})
                    break
                feedback = ("Cette requête ne renvoie aucune ligne : un filtre est sans doute faux. "
                            "Réécris-la en utilisant uniquement les indices :\n"
                            + "\n".join(f"- {h}" for h in turn.hints))
                turn.attempts.append({"sql": sql, "error": "0 ligne"})
            messages += [
                {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                {"role": "user", "content": feedback + "\nRéponds uniquement avec le JSON."},
            ]
        if result is None:
            turn.kind = "error"
            turn.text = ("Je n'ai pas réussi à construire une requête valide pour cette question. "
                         "Essayez de la reformuler.")
            return

        turn.sql, turn.df, turn.truncated = result.sql, result.df, result.truncated
        if "source_page" in result.df.columns:
            turn.source_pages = sorted({int(p) for p in result.df["source_page"].dropna()})
        turn.text = self._answer(turn)

    def _answer(self, turn: Turn) -> str:
        rows = turn.df.head(ANSWER_ROWS).to_dict(orient="records")
        payload = {
            "question": turn.question,
            "sql": turn.sql,
            "nb_lignes": len(turn.df),
            "tronque": turn.truncated or len(turn.df) > ANSWER_ROWS,
            "resultats": rows,
        }
        return self.llm.chat([
            {"role": "system", "content": prompts.ANSWER_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]).strip()


def write_trace(turn: Turn, trace_dir, model: str) -> None:
    """Append one JSON line per turn to TRACE_DIR/YYYY-MM-DD.jsonl."""
    trace_dir.mkdir(parents=True, exist_ok=True)
    record = asdict(turn)
    record["df"] = None if turn.df is None else {"rows": len(turn.df),
                                                 "columns": list(turn.df.columns)}
    now = datetime.now(UTC)
    record.update(id=uuid.uuid4().hex, ts=now.isoformat(), model=model)
    with open(trace_dir / f"{now:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
