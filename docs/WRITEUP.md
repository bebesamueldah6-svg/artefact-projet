# Write-up — Challenge AI Engineer (EDAN 2025)

**Video walkthrough:** _link to be added_

## 1. What was built

A chat app over the CEI's 35-page PDF of the 2025 legislative results. All four levels of the brief
are covered:

| Level | Delivered |
|---|---|
| 1 — Text-to-SQL | Reproducible ingestion into DuckDB, intent detection (aggregation / ranking / lookup / chart), safe SQL generation and execution, narrative + table, bar / pie / histogram charts inline. Bonus: SQL guard (SELECT-only, table **and** column allowlists, LIMIT, timeout, read-only DB), curated views, explicit non-answer policy, adversarial test set. |
| 2 — Hybrid router | Router choosing between deterministic SQL, LLM text-to-SQL and retrieval (BM25 over row-as-text chunks). Entity resolution for typos, accents, casing and party aliases. Citations: `source_page`, `row_id` and a row excerpt with every answer. |
| 3 — Agentic | Detection of ambiguous localities and scopes. The agent asks a clarifying question with clickable options and remembers the choice for the rest of the session. |
| 4 — Production | End-to-end traces with timed spans, token usage and latency. Offline evaluation by category with a grounding / citation-faithfulness metric, a metrics table and a failure list with trace ids. CI, PDF-hash dataset versioning, cache with version-based invalidation. |

## 2. Ingestion and schema decisions

**Parsing.** `pdfplumber.extract_tables()` silently dropped rows at page bottoms and split some
merged cells, so the parser works on the page geometry instead:

- every word is assigned to a column using the fixed x-boundaries of the header, which are identical on all 35 pages;
- the page is cut into constituency blocks and candidate blocks using the horizontal rulings;
- blocks split across a page break are stitched back together;
- the region labels, which are rotated 90°, are rebuilt character by character;
- repeated headers, footers and "Page N de 35" are excluded because only words inside body blocks are read.

**Validation.** Nine arithmetic checks must pass, or the pipeline fails:

- there are 205 constituencies;
- voters = invalid ballots + valid votes;
- the sum of candidate votes = valid votes − blank ballots;
- each % can be recomputed from the vote counts;
- turnout can be recomputed from voters and registered voters;
- each constituency has exactly one elected candidate, and that candidate ranks first;
- the national totals equal the sum of the constituencies;
- no names are empty.

The manifest stores the PDF's sha256. Its first 12 characters are the dataset version, used for
caching and traces.

**Schema.** Three tables follow the structure of the PDF:

- `circonscriptions`: one row per constituency, with its turnout figures;
- `candidatures`: one row per candidacy, with `rank_in_circ`, `is_elected`, `source_page` and an `excerpt` of the row as printed;
- `national_totals`.

The model never sees these tables directly. It only sees five **curated views**: `vw_results_clean`,
`vw_winners`, `vw_turnout`, `vw_turnout_region` and `vw_party_summary`, plus `national_totals`.
Joins and the seats/votes semantics are therefore defined once, and the SQL that has to be
generated stays simple.

**Normalization** (`ingest/normalize.py`):

| Function | Rule | Example |
|---|---|---|
| `norm_key` | Unicode NFKD, accents removed, upper case, punctuation → space | "Côte d’Ivoire" → `COTE D IVOIRE` |
| `party_key` | no separators at all | "R.H.D.P" = "rhdp" = `RHDP` |
| `short_locality` | splits a constituency label into its localities | 205 constituencies → 509 localities, 24 of which belong to several constituencies |

Display labels keep the PDF's original spelling.

## 3. Routing

Each request goes through `agent/router.py` in this order. Every step is a timed span in the trace.

1. **Pending clarification.** A reply such as "1" or the name of an option resolves it. The choice
   is stored in the session.
2. **Safety.** Destructive, prompt-injection and exfiltration requests are refused with an
   explanation and a safe alternative: an overview of the dataset. Out-of-scope topics (weather,
   people's roles, other elections, forecasts) get the non-answer.
3. **Entity resolution.** Fuzzy n-gram matching against localities, regions, candidates and parties,
   with party aliases and long-form names. Earlier session choices are applied here.
4. **Disambiguation.**
   - A locality that maps to several constituencies triggers a question. "Bouaké" is both 060 (ville)
     and 061 (sous-préfecture); "Bassam" matches both Grand-Bassam and Kétro-Bassam.
   - A turnout question about a multi-constituency area triggers a scope question: "Abidjan" can mean
     the district total or its 13 constituencies.
   - A locality that is unique but shares its constituency with others ("Tiapoum" → 184
     "Noé, Nouamou et Tiapoum") is resolved automatically, and the answer explains the mapping.
5. **Deterministic SQL path.** Keyword intents in French and English produce parameterised SQL over
   the views, and the answer is built from templates. This path is instant, reproducible and costs no
   tokens. Questions containing modifiers the templates do not model (average, comparison,
   "more than N", filters, margins…) skip it, so a template never answers the wrong question.
6. **LLM text-to-SQL.** The model sees the schema of the views and the resolved entity hints, and
   must return JSON: `{action, intent, sql, chart}` or `not_found`. The SQL goes through the guard.
   - A rejected or failing query is sent back to the model once, with the error, for repair.
   - An empty result although entities were recognised triggers a repair guided by the hints.
   - A second LLM call writes the answer from the returned rows only.
   - Page citations that are not backed by those rows are removed.
7. **Retrieval (RAG).** BM25 over 1,330 row-as-text chunks (candidacies and constituencies). Query
   tokens unknown to the index are mapped to their closest known token, so "Tiapum" still finds
   "Tiapoum". This path is used for lookups ("find candidate…") and as a fallback. With an LLM, the
   answer is written from the excerpts with citations; without one, the cited rows are listed.
8. **Non-answer.** The reply starts with "**Not found in the provided PDF dataset.**", followed by
   what was searched (recognised entities, retrieval score versus threshold, or the reason) and a
   suggested rephrasing.

**Why rules first?** The acceptance questions are frequent and well defined. Answering them without
an LLM makes them exact, instant and free. The LLM handles the long tail, and the app still works
fully on the supported questions when no key is configured.

## 4. Guardrails (defence in depth)

| Layer | Protection |
|---|---|
| Input | Destructive, injection and exfiltration patterns are refused before any routing. |
| Prompt | Holds only the public schema, never secrets, so prompt extraction leaks nothing. Instructions found in the question or in rows are ignored. |
| SQL guard | sqlglot parse; exactly one SELECT/CTE; table allowlist; CTEs may not shadow base tables; column allowlist; file, environment and system functions blocked; outer LIMIT capped at 300. |
| Execution | DuckDB opened read-only with `enable_external_access=false`, and a 5 s timeout through `interrupt()`. |
| Output | Charts are declarative specs rendered by Plotly. **No model-generated code is ever executed.** |

## 5. Evaluation and observability

- **Traces.** `traces/YYYY-MM-DD.jsonl` holds one record per request: route, intent, spans (safety,
  entities, disambiguation, rules, LLM calls with token usage, SQL with its validation outcome,
  retrieval hits, chart, citation check), total latency and answer. The UI shows route, intent,
  latency, tokens and the trace id under every answer.
- **Offline evaluation** (`python -m edan_chat.eval`). 50 cases across fact lookup, aggregation,
  ranking, chart, not-found, safety, disambiguation, robustness, language and LLM. Expected values
  are recomputed from the database at evaluation time.
  - **Grounding** metric: every number in an answer must exist in the rows returned for it, and
    cited pages must belong to those rows.
  - Output: a Markdown table per category and the failures with their trace ids.
  - The deterministic subset runs in CI and as a pytest regression test.
- **Results.** Deterministic paths: **45/45**, every answer grounded, p50 latency about 25 ms. Results
  with the LLM path are in `reports/eval_report.md`.

## 6. Known limitations

- The deterministic intents are keyword based. An unusual phrasing of a supported question goes to
  the LLM, or to the non-answer when no LLM is configured, rather than to a wrong template.
- The safety filter is pattern based and may refuse a legitimate question that contains a word such
  as "delete". The SQL guard remains the real barrier.
- Retrieval is lexical (BM25 + fuzzy tokens), not semantic. This is enough for names and places in
  this dataset; paraphrased narrative questions would benefit from embeddings.
- Language detection is a word-list heuristic.
- Session memory lives in the Streamlit session and is not persisted.
- `is_list` (a list versus an individual) is inferred from the name.
- The evaluation set is small and written by the author. LLM results vary with the model and the
  free-tier rate limits.

## 7. Next steps

- Add embedding-based retrieval, plus an LLM intent classifier for queries that no rule matches.
- Export traces to OpenTelemetry or Langfuse, and add dashboards for latency, routes and failures.
- Grow the evaluation set with paraphrases and adversarial variants, and add LLM-judged answer
  quality next to the exact-value checks.
- Package the app in Docker and add authentication and rate limiting for a shared deployment.
