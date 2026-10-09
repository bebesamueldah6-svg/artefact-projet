"""LLM text-to-SQL path: question + entity hints -> SQL -> guard/execute (with repair) -> grounded answer."""

from __future__ import annotations

import json

import duckdb

from edan_chat.agent import prompts
from edan_chat.agent.llm import ChatModel, parse_json
from edan_chat.agent.sql_guard import QueryResult, UnsafeSQLError, execute
from edan_chat.agent.trace import Tracer

MAX_REPAIRS = 1
ANSWER_ROWS = 30


def generate_and_run(llm: ChatModel, question: str, hints: list[str], history: list[tuple[str, str]],
                     tracer: Tracer, db_path) -> tuple[dict, QueryResult | None, list[dict]]:
    """Returns (plan, result, attempts). plan['action'] == 'not_found' -> result is None."""
    messages = [{"role": "system", "content": prompts.SQL_SYSTEM}]
    for past_q, past_sql in history[-3:]:  # short memory for follow-up questions
        messages += [{"role": "user", "content": past_q},
                     {"role": "assistant", "content": json.dumps({"action": "sql", "sql": past_sql})}]
    user = question + ("\nHints:\n" + "\n".join(f"- {h}" for h in hints) if hints else "")
    messages.append({"role": "user", "content": user})

    attempts: list[dict] = []
    plan: dict = {}
    result: QueryResult | None = None
    for attempt in range(MAX_REPAIRS + 1):
        with tracer.span("llm.generate_sql", model=getattr(llm, "model", "?"), attempt=attempt) as s:
            text, usage = llm.chat(messages, json_mode=True)
            tracer.add_usage(usage)
            s["usage"] = usage
            plan = parse_json(text)
            s["action"] = plan.get("action")
        if plan.get("action") != "sql":
            return plan, None, attempts
        sql = plan.get("sql") or ""
        with tracer.span("sql.validate_execute", sql=sql) as s:
            try:
                result = execute(sql, db_path)
                s.update(valid=True, rows=len(result.df), executed=result.sql)
            except (UnsafeSQLError, duckdb.Error) as e:
                s.update(valid=False, rejection=str(e))
                feedback = f"This query failed: {e}\nFix it."
                attempts.append({"sql": sql, "error": str(e)})
                result = None
            else:
                if not result.df.empty or not hints or attempt == MAX_REPAIRS:
                    attempts.append({"sql": sql, "error": None})
                    break
                # empty result although entities were recognised: usually an invented filter
                feedback = "This query returns no rows; a filter is probably wrong. Use only the hints:\n" + \
                    "\n".join(f"- {h}" for h in hints)
                attempts.append({"sql": sql, "error": "0 rows"})
        messages += [{"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                     {"role": "user", "content": feedback + "\nReply with the JSON only."}]
    return plan, result, attempts


def write_answer(llm: ChatModel, question: str, lang: str, result: QueryResult, tracer: Tracer) -> str:
    payload = {"question": question, "sql": result.sql, "rows_count": len(result.df),
               "truncated": result.truncated or len(result.df) > ANSWER_ROWS,
               "rows": result.df.drop(columns=["excerpt"], errors="ignore").head(ANSWER_ROWS).to_dict(orient="records")}
    language = "English" if lang == "en" else "French"
    with tracer.span("llm.write_answer", model=getattr(llm, "model", "?")) as s:
        text, usage = llm.chat([
            {"role": "system", "content": prompts.ANSWER_SYSTEM.format(language=language)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ])
        tracer.add_usage(usage)
        s["usage"] = usage
    return text.strip()


def rag_answer(llm: ChatModel, question: str, lang: str, hits: list, tracer: Tracer) -> str:
    excerpts = "\n".join(f"[{h.row_id or h.circ_id}] (p. {h.source_page}) {h.text}" for h in hits)
    language = "English" if lang == "en" else "French"
    with tracer.span("llm.rag_answer", model=getattr(llm, "model", "?")) as s:
        text, usage = llm.chat([
            {"role": "system", "content": prompts.RAG_SYSTEM.format(language=language)},
            {"role": "user", "content": f"Question: {question}\n\nExcerpts:\n{excerpts}"},
        ])
        tracer.add_usage(usage)
        s["usage"] = usage
    return text.strip()
