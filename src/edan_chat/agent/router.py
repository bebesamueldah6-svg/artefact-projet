"""Hybrid router — the agent entry point.

    question
      ├─ pending clarification? -> apply the user's choice, remember it for the session
      ├─ safety (destructive / prompt injection / exfiltration -> refuse + safe alternative;
      │          out of scope -> "Not found in the provided PDF dataset.")
      ├─ entity resolution (+ session memory) -> ambiguity? -> ask a clarifying question
      ├─ SQL path, deterministic: keyword intent -> parameterised SQL        (route = sql_rules)
      ├─ SQL path, LLM text-to-SQL with guard + repair                        (route = sql_llm)
      ├─ RAG path: BM25 over row-as-text chunks, cited                        (route = rag)
      └─ nothing grounded -> explicit non-answer                              (route = not_found)

Every step is a timed span of the request trace.
"""

from __future__ import annotations

import re

from edan_chat import config
from edan_chat.agent import charts, entities, intents, rag, safety
from edan_chat.agent.cache import dataset_version
from edan_chat.agent.lang import detect, t
from edan_chat.agent.llm import ChatModel, LLMError, OpenAICompatChat
from edan_chat.agent.sql_guard import execute
from edan_chat.agent.text2sql import generate_and_run, rag_answer, write_answer
from edan_chat.agent.trace import Tracer
from edan_chat.agent.types import NOT_FOUND, Session, Turn
from edan_chat.ingest.normalize import norm_key, short_locality

_PAGE_REF = re.compile(r"\s*\((?:p\.|page)\s*(\d+)\)", re.IGNORECASE)

LOOKUP_WORDS = (r"\bQUI EST\b", r"\bWHO IS\b", r"PARLE MOI", r"TELL ME ABOUT", r"\bINFOS?\b", r"\bFIND\b",
                r"CHERCHE", r"TROUVE", r"\bSEARCH\b", r"\bLOOK UP\b", r"\bEXISTE", r"IS THERE")
RAG_MIN_SCORE = 6.0


class Agent:
    def __init__(self, llm: ChatModel | None = None, db_path=config.DB_PATH, trace_dir=config.TRACE_DIR):
        self.llm = llm if llm is not None else OpenAICompatChat()
        self.db_path, self.trace_dir = db_path, trace_dir

    @property
    def llm_enabled(self) -> bool:
        return self.llm.available()

    # ---- public --------------------------------------------------------------------------------
    def ask(self, question: str, session: Session | None = None) -> Turn:
        session = session if session is not None else Session()
        question = question.strip()
        tracer = Tracer(question, dataset_version())
        turn = Turn(question=question, lang=detect(question), trace_id=tracer.id)
        try:
            self._route(turn, session, tracer)
        except LLMError as e:
            turn.kind, turn.route = "error", turn.route or "sql_llm"
            turn.text = t(turn.lang, f"Le modèle de langage n'a pas pu répondre ({e}).",
                          f"The language model could not answer ({e}).")
        self._finalize(turn, tracer)
        session.history.append(turn)
        return turn

    # ---- routing -------------------------------------------------------------------------------
    def _route(self, turn: Turn, session: Session, tracer: Tracer) -> None:
        question = turn.question
        if session.pending:
            resolved = self._apply_choice(turn, session, tracer)
            if resolved is None:
                return  # unrecognised choice: asked again
            question = resolved

        with tracer.span("safety") as s:
            verdict = safety.check(question, turn.lang)
            s["verdict"] = verdict.category if verdict else "ok"
        if verdict and verdict.category != "out_of_scope":
            self._refuse(turn, verdict, tracer)
            return
        if verdict:
            self._not_found(turn, tracer, reason=t(turn.lang, "la question porte sur un sujet absent du PDF "
                                                   "(autre scrutin, fonction, météo, prévision…)",
                                                   "the question is about something the PDF does not cover "
                                                   "(another election, a position, weather, a forecast…)"))
            return

        with tracer.span("entities") as s:
            matches = entities.resolve(question, self.db_path)
            matches = self._apply_memory(matches, session)
            turn.hints = [m.hint() for m in matches]
            s["matches"] = [{"kind": m.kind, "mention": m.mention, "value": m.value, "circ_ids": m.circ_ids,
                             "ambiguous": m.ambiguous} for m in matches]

        chart_type = charts.requested_type(question)
        q = intents.build_q(question, turn.lang, matches, chart_type)
        previous = next((p for p in reversed(session.history) if p.kind == "answer"), None)
        if previous and not q.has(*intents.TOPIC_WORDS) and (q.area or q.parties):
            q.text = norm_key(previous.question) + " " + q.text  # follow-up: "et à Abobo ?"

        with tracer.span("disambiguation") as s:
            options = self._ambiguity(q, turn.lang, set(session.memory))
            s["ambiguous"] = bool(options)
        if options:
            mention, choices, prompt = options
            session.pending = {"question": question, "mention": mention, "options": choices}
            turn.kind, turn.route, turn.intent = "clarify", "clarify", "disambiguation"
            turn.options, turn.text = choices, prompt
            return

        complex_q = q.has(*intents.COMPLEX)
        with tracer.span("route.rules", complex=complex_q) as s:
            plan = None if complex_q else intents.plan_for(q)
            s["intent"] = plan.intent if plan else None
        if plan:
            self._run_plan(turn, plan, q, tracer)
            return
        if complex_q and not self.llm_enabled:
            self._not_found(turn, tracer, matches=matches, reason=t(
                turn.lang, "cette question demande un calcul libre (moyenne, comparaison, filtre…) qui passe par "
                           "le modèle de langage, non configuré ici (voir .env.example)",
                "this question needs a free-form computation (average, comparison, filter…) handled by the "
                "language model, which is not configured here (see .env.example)"))
            return

        lookup = q.has(*LOOKUP_WORDS)
        with tracer.span("route.decide") as s:
            route = "rag" if lookup or not self.llm_enabled else "sql_llm"
            s.update(route=route, llm_enabled=self.llm_enabled, lookup=lookup)
        if route == "sql_llm" and self._run_llm(turn, question, chart_type, session, tracer):
            return
        self._run_rag(turn, question, matches, tracer)

    # ---- paths ---------------------------------------------------------------------------------
    def _run_plan(self, turn: Turn, plan: intents.Plan, q: intents.Q, tracer: Tracer) -> None:
        with tracer.span("sql.validate_execute", sql=plan.sql, source="rules") as s:
            result = execute(plan.sql, self.db_path)
            s.update(valid=True, rows=len(result.df), executed=result.sql)
        turn.route, turn.intent = "sql_rules", ("chart:" if q.chart else "") + plan.intent
        turn.sql, turn.df, turn.truncated = result.sql, result.df, result.truncated
        turn.attempts = [{"sql": plan.sql, "error": None}]
        if result.df.empty:
            self._not_found(turn, tracer, reason=t(turn.lang, "la requête n'a renvoyé aucune ligne",
                                                   "the query returned no rows"), keep_df=True)
            return
        turn.text = plan.say(result.df) + self._scope_note(q)
        if q.chart:
            with tracer.span("chart", requested=q.chart) as s:
                turn.chart = charts.build_spec(result.df, q.chart, turn.question, plan.chart_x, plan.chart_y)
                s["spec"] = turn.chart

    def _run_llm(self, turn: Turn, question: str, chart_type: str | None, session: Session,
                 tracer: Tracer) -> bool:
        history = [(p.question, p.sql) for p in session.history if p.sql and p.kind == "answer"]
        plan, result, attempts = generate_and_run(self.llm, question, turn.hints, history, tracer, self.db_path)
        turn.route, turn.attempts = "sql_llm", attempts
        if plan.get("action") == "not_found":
            turn.intent = "not_found"
            return False  # let retrieval try before giving up
        if result is None:
            turn.kind = "error"
            turn.text = t(turn.lang, "Je n'ai pas réussi à construire une requête valide. Essayez de reformuler.",
                          "I could not build a valid query. Please rephrase.")
            return True
        turn.intent = plan.get("intent") or "sql"
        turn.sql, turn.df, turn.truncated = result.sql, result.df, result.truncated
        if result.df.empty:
            self._not_found(turn, tracer, reason=t(turn.lang, "la requête générée n'a renvoyé aucune ligne",
                                                   "the generated query returned no rows"), keep_df=True)
            return True
        turn.text = write_answer(self.llm, question, turn.lang, result, tracer)
        with tracer.span("citations.verify") as s:
            turn.text, s["removed"] = strip_unsupported_pages(turn.text, result.df)
        spec = plan.get("chart") or {}
        wanted = chart_type or (spec.get("type") if spec.get("type") not in (None, "none") else None)
        if wanted:
            with tracer.span("chart", requested=wanted) as s:
                turn.chart = charts.build_spec(result.df, wanted, question, spec.get("x"), spec.get("y"))
                s["spec"] = turn.chart
        return True

    def _run_rag(self, turn: Turn, question: str, matches: list, tracer: Tracer) -> None:
        with tracer.span("retrieval", top_k=config.RAG_TOP_K) as s:
            hits = rag.search(question, db_path=self.db_path)
            s["hits"] = [{"doc_id": h.doc_id, "score": h.score, "page": h.source_page} for h in hits]
        strong = [h for h in hits if h.score >= RAG_MIN_SCORE]
        if not strong:
            self._not_found(turn, tracer, matches=matches, hits=hits)
            return
        turn.route, turn.intent = "rag", "lookup"
        turn.citations = [{"source_page": h.source_page, "row_id": h.row_id, "excerpt": h.text} for h in strong[:5]]
        if self.llm_enabled:
            turn.text = rag_answer(self.llm, question, turn.lang, strong[:5], tracer)
            if turn.text.startswith(NOT_FOUND):
                turn.kind = "not_found"
        else:
            turn.text = t(turn.lang, "Lignes du PDF les plus pertinentes :", "Most relevant rows of the PDF:") + \
                "\n" + "\n".join(f"- {h.text}" for h in strong[:5])

    # ---- refusals, non-answers, clarifications ----------------------------------------------------
    def _refuse(self, turn: Turn, verdict: safety.Verdict, tracer: Tracer) -> None:
        turn.kind, turn.route, turn.intent = "refuse", "safety", verdict.category
        turn.text = verdict.message
        if verdict.safe_alternative == "dataset_overview":
            with tracer.span("sql.validate_execute", sql="dataset_overview", source="safe_alternative") as s:
                result = execute(safety.OVERVIEW_SQL, self.db_path)
                s.update(valid=True, rows=len(result.df))
            turn.sql, turn.df = result.sql, result.df

    def _not_found(self, turn: Turn, tracer: Tracer, reason: str = "", matches=None, hits=None,
                   keep_df: bool = False) -> None:
        L = turn.lang
        turn.kind, turn.route = "not_found", "not_found"
        if not keep_df:
            turn.df, turn.sql = None, None
        searched = []
        if matches:
            searched.append(t(L, "entités reconnues : ", "recognised entities: ")
                            + ", ".join(f"{m.value} ({m.kind})" for m in matches))
        if turn.hints and not matches:
            searched.append(t(L, "indices : ", "hints: ") + "; ".join(turn.hints))
        if hits is not None:
            best = hits[0].score if hits else 0
            searched.append(t(L, f"recherche plein texte dans les {len(rag.get_index(str(self.db_path), dataset_version()).docs)} lignes du PDF (meilleur score {best:.1f}, seuil {RAG_MIN_SCORE})",
                              f"full-text search over the {len(rag.get_index(str(self.db_path), dataset_version()).docs)} PDF rows (best score {best:.1f}, threshold {RAG_MIN_SCORE})"))
        if reason:
            searched.insert(0, reason)
        if not searched:
            searched.append(t(L, "aucune intention ni entité reconnue", "no known intent or entity recognised"))
        turn.text = (f"**{NOT_FOUND}**\n\n" + t(L, "Ce qui a été cherché : ", "What was searched: ")
                     + "; ".join(searched) + ".\n\n" + t(
                         L, "Suggestion : posez une question sur les résultats des législatives 2025 — sièges par "
                            "parti, gagnant d'une circonscription, participation, classements (ex. « Qui a gagné à "
                            "Yopougon ? », « Participation par région »).",
                         "Suggestion: ask about the 2025 legislative results — seats per party, a constituency's "
                         "winner, turnout, rankings (e.g. \"Who won in Yopougon?\", \"Participation rate by region\")."))

    def _ambiguity(self, q: intents.Q, lang: str, remembered: set[str]):
        """Return (mention, options, prompt) when the question needs a clarification."""
        for m in q.circ:
            if m.ambiguous and m.mention not in remembered:
                opts = [{"label": f"{entities.circ_label(c, self.db_path)} ({c})", "value": {"circ_ids": [c]}}
                        for a in m.alternatives for c in a["circ_ids"]]
                opts.append({"label": t(lang, "Toutes ces circonscriptions", "All of these constituencies"),
                             "value": {"circ_ids": m.circ_ids}})
                prompt = t(lang, f"« {m.mention.title()} » correspond à plusieurs circonscriptions. Laquelle ?",
                           f"\"{m.mention.title()}\" matches several constituencies. Which one?")
                return m.mention, opts, prompt
        for m in q.regions:
            if m.mention in remembered:
                continue
            explicit = q.has(r"\bREGION", r"DISTRICT", *intents.BY_REGION, *intents.BY_CIRC)
            if q.has(*intents.TURNOUT) and not explicit and not q.chart:
                n = len(self._region_circs(m.value))
                opts = [{"label": t(lang, f"{m.value} — total agrégé", f"{m.value} — aggregated total"),
                         "value": {"region": m.value}},
                        {"label": t(lang, f"Détail par circonscription ({n})", f"Breakdown by constituency ({n})"),
                         "value": {"circ_ids": self._region_circs(m.value)}}]
                prompt = t(lang, f"« {m.mention.title()} » : voulez-vous le total de « {m.value} » ou le détail "
                                 f"de ses {n} circonscriptions ?",
                           f"\"{m.mention.title()}\": do you want the total for \"{m.value}\" or the breakdown of "
                           f"its {n} constituencies?")
                return m.mention, opts, prompt
        return None

    def _apply_choice(self, turn: Turn, session: Session, tracer: Tracer) -> str | None:
        pending = session.pending
        reply = norm_key(turn.question)
        chosen = None
        if reply.isdigit() and 1 <= int(reply) <= len(pending["options"]):
            chosen = pending["options"][int(reply) - 1]
        else:
            chosen = next((o for o in pending["options"] if reply and reply in norm_key(o["label"])), None)
        with tracer.span("clarification.resolve", reply=turn.question) as s:
            s["chosen"] = chosen["label"] if chosen else None
        if chosen is None:
            session.pending = None  # treat as a new question
            return turn.question
        session.memory[pending["mention"]] = chosen["value"]
        session.pending = None
        turn.intent = "clarification_choice"
        turn.lang = detect(pending["question"])
        return pending["question"]

    def _apply_memory(self, matches: list, session: Session) -> list:
        """Replace an ambiguous mention by the option the user picked earlier in the session."""
        out = []
        for m in matches:
            choice = session.memory.get(m.mention)
            if choice and "circ_ids" in choice:
                out.append(entities.Match("circonscription", m.mention, m.value, m.score, choice["circ_ids"]))
            elif choice and m.kind == "circonscription":
                continue  # the user chose the region reading of this mention
            else:
                out.append(m)
        return out

    def _region_circs(self, region: str) -> list[str]:
        sql = f"SELECT circ_id FROM vw_turnout WHERE region = {intents.sq([region])} ORDER BY circ_id"
        return execute(sql, self.db_path).df["circ_id"].tolist()

    def _scope_note(self, q: intents.Q) -> str:
        """Explain how a locality was mapped when it is one of several in its constituency (Tiapoum)."""
        notes = []
        for m in q.circ:
            if len(m.circ_ids) == 1:
                label = entities.circ_label(m.circ_ids[0], self.db_path)
                if len(short_locality(label)) > 1:
                    notes.append(t(q.lang, f"« {m.mention.title()} » fait partie de la circonscription {m.circ_ids[0]} "
                                           f"« {label} » (un seul siège pour l'ensemble).",
                                   f"\"{m.mention.title()}\" belongs to constituency {m.circ_ids[0]} \"{label}\" "
                                   f"(one seat for the whole constituency)."))
        return ("\n\n_" + " ".join(notes) + "_") if notes else ""

    # ---- trace -----------------------------------------------------------------------------------
    def _finalize(self, turn: Turn, tracer: Tracer) -> None:
        if not turn.citations and turn.df is not None and "source_page" in turn.df.columns:
            cols = [c for c in ("source_page", "row_id", "excerpt") if c in turn.df.columns]
            seen, cites = set(), []
            for rec in turn.df[cols].head(50).to_dict(orient="records"):
                key = rec.get("row_id") or rec["source_page"]
                if key not in seen:
                    seen.add(key)
                    cites.append({"source_page": int(rec["source_page"]), "row_id": rec.get("row_id"),
                                  "excerpt": rec.get("excerpt")})
            turn.citations = cites[:10]
        turn.usage = dict(tracer.usage)
        turn.latency_ms = tracer.elapsed_ms
        tracer.write(self.trace_dir, lang=turn.lang, route=turn.route, intent=turn.intent, kind=turn.kind,
                     sql=turn.sql, rows=None if turn.df is None else len(turn.df), chart=turn.chart,
                     citations=[{k: c[k] for k in ("source_page", "row_id")} for c in turn.citations],
                     answer=turn.text[:2000])


def strip_unsupported_pages(text: str, df) -> tuple[str, list[int]]:
    """Remove "(p. N)" references that are not backed by a source_page of the returned rows."""
    pages = set() if df is None or "source_page" not in df.columns else {int(p) for p in df["source_page"].dropna()}
    removed: list[int] = []

    def keep(m: re.Match) -> str:
        if int(m.group(1)) in pages:
            return m.group(0)
        removed.append(int(m.group(1)))
        return ""
    return _PAGE_REF.sub(keep, text), removed
