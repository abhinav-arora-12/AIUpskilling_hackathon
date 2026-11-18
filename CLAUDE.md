# CLAUDE.md: The Unreliable Assistant (Hackathon Mid-Check)

Written for: Claude Code sessions working in this repo.

**Assumptions (read first)**
- [ASSUMPTION] Team size, deadline, and hours available are unknown. See design.md §11.
- [ASSUMPTION] Judging weights are not in the brief. Priorities in §8 are inferred.
- [ASSUMPTION] Python 3.11 with `uv`, implied by `mcp_config.json` in the brief.
- [LOCKED] Gate 1 file is `design.md` (lowercase, per brief). Decision D1.
- [TO CONFIRM] The knowledge corpus source and any dataset restrictions.

## 1. Project Summary
We are building an enterprise-grade, MCP-based agentic RAG assistant that answers policy and operations questions with grounded citations. It abstains when evidence is weak and routes risky actions to human approval. Primary users are enterprise operations and compliance staff. Reviewers approve Tier 3 actions. The demo is a Streamlit developer workbench backed by a live agent API.

## 2. Hackathon Context
- **Problem:** fix "The Unreliable Assistant". Known failures: hallucinations, unbounded tool loops, cross-tenant cache leaks, context drift, latency collapse, token bloat, PII and injection exposure, and no HITL gate.
- **Mandatory order:** (1) `design.md` locked, then (2) `docs/workflow_orchestration.png` at 300 DPI, then (3) application code. Do not write app code before (1) and (2) exist.
- **Headline targets:** p95 ≤ 450 ms, p99 < 600 ms at 50K VUs (scaled plan in DESIGN §12). RAGAS Faithfulness ≥ 0.92, Answer Relevancy ≥ 0.90, Context Precision ≥ 0.88, Context Recall ≥ 0.85. The 36 KPIs are listed in brief Gate 12; DESIGN §1 covers the demo subset.
- **Judging criteria:** [TO CONFIRM], not stated in the brief.
- **Deadline:** [TO CONFIRM].

## 3. Tech Stack
- Python 3.11 (pinned), managed with `uv`.
- FastAPI + Uvicorn (async). [TO CONFIRM versions]
- MCP Python SDK (`mcp>=1.2,<2`, FastMCP v1 API): JSON-RPC 2.0 over stdio (all three servers, D21).
- Pydantic v2 for all tool and API models.
- Retrieval: rank-bm25 (BM25Okapi), nltk (Porter stemmer), FAISS (CPU, HNSW), BGE-large-en-v1.5 and bge-reranker-v2-m3 INT8 via `fastembed` (ONNX Runtime, no torch).
- ONNX Runtime.
- Storage: PostgreSQL + pgvector, Redis. [ASSUMPTION] SQLite is the fallback. The brief mentions both.
- Plugins: pluggy.
- Observability: OpenTelemetry SDK → OTel Collector → Prometheus, Tempo, Grafana.
- Evaluation: ragas.
- LLM provider: OpenRouter. Generation `anthropic/claude-sonnet-5.5`, triage `anthropic/claude-haiku-5.5` (design.md D25, D29). Optional Anthropic alternative. The LLM never creates Tier 3 requests (D26).
- UI: Streamlit. Diagram: `graphviz` Python package plus the Graphviz binary.
- Load testing: k6 and JMeter. [TO CONFIRM] whether JMeter is required on the demo machine.
- Knowledge graph: Graphify CLI [TO CONFIRM it is available]. Vault notes are Obsidian-flavored Markdown.

## 4. Project Structure (as built)
```
design.md                        Gate 1 blueprint, LOCKED; decisions D1-D24, measured timings
CLAUDE.md                        this file
pyproject.toml                   deps (Python 3.11, uv); ruff and pytest config
.env.example                     variable names only, no values
docs/workflow.dot                Gate 2 source; renders docs/workflow_orchestration.png
mcp_config.json                  MCP server and plugin registration (descriptive; matches the code)
mcp_servers/rag_server.py        mcp-hybrid-rag: hybrid_retrieve tool, rag://tenants/{t}/chunks/{id}
mcp_servers/state_server.py      mcp-state-store: facts, approvals (HITL), audit (hash chain)
mcp_servers/graphify_server.py   mcp-graphify-vault: graph_subgraph (depth 1-2, tenant-filtered)
app/main.py                      FastAPI routes: query, HITL, health, traces, kpi, breaker reset
app/config.py                    all thresholds and budgets (brief Gates 3, 4, 9)
app/agent/gateway.py             orchestrator: hooks, cache, triage, routes, bounded loop, MCP hub
app/agent/triage.py              rules-first classifier: intent, domain, risk tier, route
app/agent/harness.py             tool harness (validate, auto-repair, RBAC, idempotency), circuit breaker
app/agent/generate.py            extractive answerer, LLM answerer (optional), lexical verifier
app/rag/index.py                 vault parsing, chunking, BM25 tokens, BGE embeddings, FAISS HNSW
app/rag/retriever.py             3-stage retrieval: BM25, dense, RRF, reranker, abstention
app/plugins/hooks.py             PII redactor, injection shield, chunk scan, hook registry
app/cache/semantic.py            tenant+user scoped semantic cache, TTL with jitter
app/state_store.py               SQLite state (tenant_id everywhere), approvals, audit chain
app/graph.py                     wikilink graph and depth-limited subgraphs (D19)
app/telemetry.py                 span recorder with brief span names -> data/traces.jsonl
app.py                           Streamlit workbench, six tabs
knowledge_vault/                 12 synthetic atomic notes (frontmatter + [[wikilinks]])
data/index/                      built index (FAISS, vectors, chunks) [generated]
data/golden/golden.jsonl         50 synthetic golden questions (D13)
data/redteam/redteam.jsonl       100 synthetic red-team prompts, 5 categories (D13)
data/results/                    run outputs: golden, red team, probes, k6, kpis.json [generated]
scripts/build_index.py           build the index from knowledge_vault/
scripts/make_datasets.py         generate golden and red-team sets (deterministic)
scripts/run_eval.py              golden run through the gateway; triage and repair sets
scripts/run_redteam.py           red-team run through the HTTP API
scripts/probes.py                cache replay and isolation, chaos loop, HITL, MCP overhead
scripts/scorecard.py             builds the 36-KPI scorecard (PASS / FAIL / NOT_MEASURED)
load_tests/k6_50k_load.js        k6 script; scaled with PEAK_VUS (D11, D23)
tests/                           unit (guards, harness, cache, triage, state, loop) and integration
```

## 5. Commands
```
uv sync                                          # install pinned deps (Python 3.11)
cp .env.example .env                             # fill secrets locally; never commit .env
uv run python scripts/build_index.py             # vault -> BM25 + FAISS index (first run downloads models)
uv run uvicorn app.main:app --port 8000          # API; starts the three MCP servers as children
uv run streamlit run app.py                      # workbench (needs the API running)
uv run pytest -q                                 # all tests (~35 s; integration starts MCP servers)
uv run ruff check .                              # lint (rules pinned in pyproject.toml)
uv run python scripts/make_datasets.py           # regenerate golden and red-team sets
uv run python scripts/run_eval.py               # golden run (~7 s per RAG query on this host)
uv run python scripts/run_redteam.py             # 100-prompt red team through the API
uv run python scripts/probes.py                  # cache, chaos, HITL, MCP overhead probes
PEAK_VUS=20 k6 run --summary-export data/results/k6_summary.json load_tests/k6_50k_load.js
echo '{"peak_vus": 20}' > data/results/load_meta.json   # the scorecard reads the VU count from here
uv run python scripts/scorecard.py              # writes data/results/kpis.json
dot -Tpng -Gdpi=300 docs/workflow.dot -o docs/workflow_orchestration.png   # Gate 2
```
Deploy: not in scope for the demo. No containers are used (Docker is not available on this host).

## 6. Coding Conventions
- Format and lint with ruff. Line length 100. [ASSUMPTION]
- Full type hints. Pydantic v2 models for every MCP tool input and output, and every API body.
- Names: `snake_case` for functions and modules, `PascalCase` for classes, `UPPER_SNAKE` for constants. Span and KPI names match DESIGN exactly (e.g., `rag.bm25`).
- Errors: never swallow exceptions. MCP tools return typed error codes from DESIGN §7. PII and injection plugins fail closed.
- Logs: structured JSON, always with `trace_id` and `tenant_id`. Never log raw PII or full user prompts.
- Tenant isolation: every DB query and every Redis key includes `tenant_id`. Cache keys use `SHA256(tenant_id:user_id:norm_query)`.
- Comments explain why. When enforcing a threshold, cite its KPI or gate number, e.g. `# KPI-09`.
- No magic numbers outside `app/config.py`.
- Tests: pytest, one test module per source module.

## 7. Workflow Rules
- **Plan before coding** on any non-trivial task. State the approach, files touched, and tests to add. If the change alters a DESIGN contract (schema, threshold, error code), update design.md first and say so.
- **Gate order:** design.md locked → Gate 2 PNG → app code.
- **Small, incremental changes.** One behavior per change. Keep diffs reviewable.
- **Run tests after each change.** Run the full `pytest` before calling a task done. Report failures verbatim, not summarized.
- **Ask before adding a dependency.** Justify it and update `pyproject.toml` in the same change.
- **Never commit secrets.** `.env` is git-ignored. Only `.env.example` (names, no values) is committed. Never put keys in code, tests, fixtures, or logs.
- **Commit only when asked.** Commit trailers follow the team's attribution settings [TO CONFIRM].

## 8. Priorities
Order: **working demo > polish > extras.** Never trade a working demo path for an extra.

| Rank | Priority | Likely judging criterion [ASSUMPTION] | Concrete meaning |
|---|---|---|---|
| 1 | Working end-to-end demo | Functionality | Query → triage → RAG → cited answer or abstention. HITL approve/reject works live. |
| 2 | Measured evidence | Technical depth | Real numbers from golden set, red team, and a load run, shown in workbench tab 6. |
| 3 | Safety and governance | Security and compliance | PII redaction, injection shield, tenant isolation, HITL gate. |
| 4 | Polish | Presentation | Tabs 1, 2, 3, 5 are clean. Architecture diagram renders. |
| 5 | Extras | Innovation | Graphify pruning, OTel waterfall, token-savings gauge. |

## 9. Out of Scope
- Autonomous clinical, financial, or legal execution. Brief's hard boundary: HITL required.
- Multi-region database replication, LoRA or domain fine-tuning, self-hosted vLLM or TensorRT-LLM. Roadmap only.
- A real 50K-VU run without matching infrastructure. Use the scaled plan in DESIGN §12.
- Live web browsing from agents.
- Custom auth, SSO, or user-management UI.
- Training custom ML models. Use only the named pretrained models.

## 10. Known Constraints
- Mandatory gates: design.md before app code; 300 DPI PNG before app logic.
- Stage budgets: BM25 < 12 ms, dense < 25 ms, RRF < 3 ms, rerank < 45 ms, triage ≤ 25 ms, loop total 450 ms, max 5 ReAct steps, 120 ms per step.
- Reranker abstention threshold: 0.42. Below it, return the deterministic abstention response.
- Golden set: 250 questions stratified 25% Factoid, 25% Complex Reasoning, 20% Multi-Hop, 15% Summary, 15% Negative/Unanswerable. [ASSUMPTION] For the demo, a 50-question subset is acceptable, stated as such.
- Red team: 100 prompts across five categories, per brief Gate 14.
- LLM rate limits and 429 handling: [TO CONFIRM] provider limits.
- Corpus and dataset rules from organizers: [TO CONFIRM].
- Load-test hardware: [TO CONFIRM].
- API keys come from `.env` only.
