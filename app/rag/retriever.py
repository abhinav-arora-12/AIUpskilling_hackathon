"""Three-stage hybrid retrieval (design.md §3 stage table, brief Gate 3).

Stage 1: BM25Okapi + Porter stemming, top 50.
Stage 2: BGE-large dense embeddings + FAISS HNSW, top 50.
Fusion:  Reciprocal Rank Fusion (k=60), dedupe by chunk hash, pool 40.
Stage 3: BGE reranker v2-m3 (INT8), top rerank_k with sigmoid score; abstain below threshold.

Tenant entitlement is applied before fusion, so one tenant never sees another tenant's chunks.
"""

from __future__ import annotations

import math
import re
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

from app import config as C
from app.rag.index import Chunk, load_index

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STEM = PorterStemmer()


def tokenize(text: str) -> list[str]:
    return [_STEM.stem(t) for t in _TOKEN_RE.findall(text.lower())]


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=C.RERANK_MODEL)


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=C.EMBED_MODEL)


def embed_query(query: str) -> np.ndarray:
    """Unit-normalized BGE query vector. Shared by the semantic cache and stage 2."""
    qvec = np.array(list(_embedder().embed([query])), dtype="float32")
    return (qvec / max(np.linalg.norm(qvec), 1e-12))[0]


class HybridRetriever:
    def __init__(self, index_dir: Path = C.PATHS.index_dir):
        self.chunks, self.vectors, self.dense = load_index(index_dir)
        self.tokens = [tokenize(c.text) for c in self.chunks]
        self.dense_ok = True

    def _entitled(self, tenant_id: str) -> np.ndarray:
        return np.array([tenant_id in c.tenants for c in self.chunks], dtype=bool)

    def stage1_bm25(self, query: str, mask: np.ndarray, k1: float, b: float, top_k: int):
        t0 = time.perf_counter()
        bm25 = BM25Okapi(self.tokens, k1=k1, b=b)
        scores = np.asarray(bm25.get_scores(tokenize(query)), dtype="float32")
        scores[~mask] = -np.inf
        order = [i for i in np.argsort(-scores)[:top_k] if np.isfinite(scores[i]) and scores[i] > 0]
        return [(int(i), float(scores[i])) for i in order], (time.perf_counter() - t0) * 1000

    def stage2_dense(self, query: str, mask: np.ndarray, top_k: int, qvec: np.ndarray | None = None):
        t0 = time.perf_counter()
        if qvec is None:
            qvec = embed_query(query)
        # Search wide, then filter by entitlement, then keep top_k. Corpus is small, so this is exact enough.
        k_search = min(len(self.chunks), top_k * 4)
        sims, idxs = self.dense.search(np.asarray(qvec, dtype="float32").reshape(1, -1), k_search)
        out = [
            (int(i), float(s))
            for s, i in zip(sims[0], idxs[0], strict=False)
            if i >= 0 and mask[i]
        ][:top_k]
        return out, (time.perf_counter() - t0) * 1000

    @staticmethod
    def rrf(lists: list[list[int]], k: int, pool: int) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for ranked in lists:
            for rank, idx in enumerate(ranked, start=1):
                scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
        return sorted(scores.items(), key=lambda kv: -kv[1])[:pool]

    def retrieve(
        self,
        query: str,
        tenant_id: str,
        *,
        k1: float = C.BM25_K1,
        b: float = C.BM25_B,
        rrf_k: int = C.RRF_K,
        pool: int = C.RRF_POOL,
        rerank_k: int = C.RERANK_K,
        threshold: float = C.ABSTAIN_THRESHOLD,
        top_k: int = C.STAGE_TOP_K,
        use_reranker: bool = True,
        qvec: np.ndarray | None = None,
    ) -> dict:
        mask = self._entitled(tenant_id)
        timings: dict[str, float] = {}

        bm25_hits, timings["bm25_ms"] = self.stage1_bm25(query, mask, k1, b, top_k)
        dense_hits, timings["dense_ms"] = self.stage2_dense(query, mask, top_k, qvec)

        t0 = time.perf_counter()
        bm25_scores = dict(bm25_hits)
        dense_scores = dict(dense_hits)
        fused = self.rrf(
            [[i for i, _ in bm25_hits], [i for i, _ in dense_hits]], rrf_k, pool
        )
        timings["rrf_ms"] = (time.perf_counter() - t0) * 1000

        candidates = []
        for rank, (idx, rrf_score) in enumerate(fused, start=1):
            chunk: Chunk = self.chunks[idx]
            candidates.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "title": chunk.title,
                    "text": chunk.text,
                    "bm25": round(bm25_scores.get(idx, 0.0), 4),
                    "cosine": round(dense_scores.get(idx, 0.0), 4),
                    "rrf_score": round(rrf_score, 6),
                    "rrf_rank": rank,
                    "rerank_score": None,
                }
            )

        reranker_unavailable = False
        if use_reranker and candidates:
            t0 = time.perf_counter()
            try:
                raw = list(_reranker().rerank(query, [c["text"] for c in candidates]))
                for cand, score in zip(candidates, raw, strict=True):
                    cand["rerank_score"] = round(1.0 / (1.0 + math.exp(-float(score))), 4)
            except Exception:  # R4 plan B: fall back to RRF order, flagged
                reranker_unavailable = True
            timings["rerank_ms"] = (time.perf_counter() - t0) * 1000
        else:
            reranker_unavailable = True

        if not reranker_unavailable:
            candidates.sort(key=lambda c: -c["rerank_score"])
            top = candidates[:rerank_k]
            top_score = top[0]["rerank_score"] if top else 0.0
        else:
            # Without the reranker, the best available signal is the RRF order.
            top = candidates[:rerank_k]
            top_score = 0.0

        return {
            "query": query,
            "tenant_id": tenant_id,
            "candidates": top,
            "all_candidates": candidates,
            "top_score": top_score,
            "threshold": threshold,
            "abstain": (not reranker_unavailable and top_score < threshold) or not top,
            "reranker_unavailable": reranker_unavailable,
            "timings_ms": {k: round(v, 2) for k, v in timings.items()},
            "stage_counts": {"bm25": len(bm25_hits), "dense": len(dense_hits), "fused": len(fused)},
        }


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    return HybridRetriever()
