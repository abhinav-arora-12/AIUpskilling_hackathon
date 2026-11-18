"""All thresholds and budgets live here (CLAUDE.md §6). Values follow design.md §3-§8."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Models (design.md D6, D7). Local models run without an API key.
EMBED_MODEL = "BAAI/bge-large-en-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3-int8"
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-5-5")
GENERATION_MODEL = os.environ.get("GENERATION_MODEL", "claude-sonnet-5-5")

# Retrieval parameters (brief Gate 3)
BM25_K1 = 1.25
BM25_B = 0.75
STAGE_TOP_K = 50  # BM25 and dense candidates each
RRF_K = 60
RRF_POOL = 40
RERANK_K = 4
ABSTAIN_THRESHOLD = 0.42  # sigmoid of cross-encoder score
HNSW_M = 32
HNSW_EF_SEARCH = 64
CHUNK_TOKENS = 512  # approximated as words (see chunking.py)
CHUNK_OVERLAP = 64

# Latency budgets in ms (brief Gates 3, 4; KPI-01/05/07/08/09)
BUDGET_PRE_HOOK_MS = 15
BUDGET_TRIAGE_MS = 25
BUDGET_BM25_MS = 12
BUDGET_DENSE_MS = 25
BUDGET_RRF_MS = 3
BUDGET_RERANK_MS = 45
BUDGET_LOOP_TOTAL_MS = 450
BUDGET_LOOP_STEP_MS = 120
BUDGET_E2E_P95_MS = 450

# Loop limits (brief Gate 4)
MAX_LOOP_ITERATIONS = 5

# Cache (brief Gate 9, KPI-27)
CACHE_SEMANTIC_THRESHOLD = 0.92
CACHE_TTL_S = 300
CACHE_TTL_JITTER_S = 45

# Circuit breaker (brief Gate 9)
BREAKER_MAX_IDENTICAL_FAILURES = 2

# Verification: share of an answer sentence's content words that must appear in its cited chunk
VERIFY_MIN_OVERLAP = 0.5

# Compression: observations truncated to this many words (brief Gate 5, ~180 tokens)
OBSERVATION_MAX_WORDS = 180
SLIDING_WINDOW_TURNS = 3


@dataclass(frozen=True)
class Paths:
    vault: Path = ROOT / "knowledge_vault"
    index_dir: Path = ROOT / "data" / "index"
    golden: Path = ROOT / "data" / "golden" / "golden.jsonl"
    redteam: Path = ROOT / "data" / "redteam" / "redteam.jsonl"
    results: Path = ROOT / "data" / "results"
    state_db: Path = ROOT / "data" / "state.sqlite3"
    traces: Path = ROOT / "data" / "traces.jsonl"
    graph: Path = ROOT / "data" / "index" / "graph.json"
    dot: Path = ROOT / "docs" / "workflow.dot"
    png: Path = ROOT / "docs" / "workflow_orchestration.png"


@dataclass
class Settings:
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None)
    default_tenant: str = field(default_factory=lambda: os.environ.get("DEFAULT_TENANT_ID", "tenant_a"))


PATHS = Paths()
SETTINGS = Settings()
