# edan-chat — chat with the EDAN 2025 election results

A minimal "chat with your data" web app over the official results of the 2025 Ivorian legislative
elections, published by the CEI as a 35-page PDF
([EDAN_2025_RESULTAT_NATIONAL_DETAILS.pdf](https://www.cei.ci/wp-content/uploads/2025/12/EDAN_2025_RESULTAT_NATIONAL_DETAILS.pdf)).
Ask in **English or French**; answers come **only** from the PDF, with the table, the SQL, the PDF
page citations and, on request, a chart.

> "How many seats did RHDP win?" · "Top 10 candidates by score in region Poro" ·
> "Participation rate by region" · "Histogram of winners by party" · "Qui a gagné à Bouaké ?"

Design notes, schema decisions and limitations: **[docs/WRITEUP.md](docs/WRITEUP.md)**.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed automatically).

```bash
uv sync
cp .env.example .env                  # optional: add a free Groq or Gemini key (see below)
uv run python -m edan_chat.ingest     # PDF -> parse -> Parquet/CSV + DuckDB -> 9 consistency checks
uv run streamlit run src/edan_chat/app.py
```

`uv run edan-chat` gives the same agent in the terminal.

### LLM configuration (`.env`)

| `LLM_PROVIDER` | Key | Default model | Notes |
|---|---|---|---|
| `groq` (default) | `GROQ_API_KEY` — free at console.groq.com | `openai/gpt-oss-120b` | fast (1–3 s) |
| `gemini` | `GEMINI_API_KEY` — free at aistudio.google.com | `gemini-2.5-flash` | |
| `ollama` | none | `qwen2.5:7b` | fully local, slow on CPU |
| `none` | — | — | deterministic paths only |

Any OpenAI-compatible endpoint works through `LLM_BASE_URL` / `LLM_MODEL`. **Without a key the app
still works**: the supported question types (all acceptance questions of the brief) are answered by
the deterministic SQL path; only free-form analytics need the LLM.

## How it works

```
PDF ──ingest──▶ circonscriptions (205) + candidatures (1,125) + national_totals ──▶ DuckDB + curated views
                                         (9 arithmetic consistency checks, manifest with PDF sha256)

question ─▶ safety ─▶ entities ─▶ disambiguation ─▶ SQL rules ─▶ LLM text-to-SQL ─▶ RAG ─▶ "Not found…"
            refuse     typos,      ask the user       keyword       guard + repair     BM25 over
            unsafe     aliases,    (Bouaké, Abidjan)  intents       + grounded answer  row-as-text
            requests   memory                         (instant)                        chunks, cited
```

| Step | Module |
|---|---|
| PDF parsing (geometry-based, handles headers/footers, page breaks, rotated region labels) | `ingest/parse_pdf.py` |
| Normalization (accents, casing, party keys, locality splitting) | `ingest/normalize.py` |
| Schema, curated views, manifest | `ingest/build_db.py` |
| Consistency checks | `ingest/validate.py` |
| Router (orchestration, non-answer policy, clarification, session memory) | `agent/router.py` |
| Input guardrails (destructive, prompt injection, exfiltration, out of scope) | `agent/safety.py` |
| Entity resolution (fuzzy, aliases, ambiguity detection) | `agent/entities.py` |
| Deterministic SQL intents (FR/EN templates) | `agent/intents.py` |
| LLM text-to-SQL + grounded answer | `agent/text2sql.py`, `agent/prompts.py`, `agent/llm.py` |
| SQL guard (SELECT-only, table + column allowlist, LIMIT, timeout, read-only DB) | `agent/sql_guard.py` |
| Retrieval (BM25, typo-tolerant, provenance) | `agent/rag.py` |
| Charts (declarative spec, rendered with Plotly) | `agent/charts.py` |
| Tracing (timed spans, tokens, latency → `traces/*.jsonl`) | `agent/trace.py` |
| Cache (LLM responses keyed by dataset version) | `agent/cache.py` |
| Offline evaluation | `eval.py` |

## Quality

```bash
uv run pytest                                  # 36 tests, no LLM needed
uv run python -m edan_chat.eval --no-llm       # deterministic paths: 45 cases
uv run python -m edan_chat.eval                # + LLM text-to-SQL cases (needs a key)
```

The evaluation recomputes every expected value from the database (`truth_sql`), checks routing,
charts, refusals, clarifications, and **grounding**: every number in an answer must appear in the
rows returned for it, and cited pages must be pages of those rows. Reports land in `reports/`.
CI (`.github/workflows/ci.yml`) rebuilds the dataset from the PDF, then runs lint, tests and the
deterministic evaluation on every push.

Latest results: see [reports/](reports/).

## Repository layout

```
data/raw/        the CEI PDF (input)
data/processed/  Parquet / CSV exports + manifest.json (the DuckDB file is rebuilt by ingest)
src/edan_chat/   ingest/, agent/, app.py, eval.py, config.py
tests/           unit + regression tests
reports/         evaluation reports
docs/WRITEUP.md  design write-up
```
