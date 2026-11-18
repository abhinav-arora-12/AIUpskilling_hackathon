"""Tenant-scoped semantic cache (brief Gate 9, KPI-27/28, design.md D5).

- Exact key: SHA256(tenant_id:user_id:norm_query).
- Semantic match: cosine >= 0.92 against cached queries in the same tenant AND user bucket only.
- TTL: 300 s +/- 45 s jitter, so entries do not expire together (stampede guard).

In-process by default. Redis is not implemented in this build (see design.md D5 note).
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass

import numpy as np

from app import config as C


def norm_query(text: str) -> str:
    return " ".join(text.lower().split())


def cache_key(tenant_id: str, user_id: str, query: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{user_id}:{norm_query(query)}".encode("utf-8")).hexdigest()


@dataclass
class Entry:
    key: str
    tenant_id: str
    user_id: str
    query: str
    vector: np.ndarray
    payload: dict
    expires_at: float


class SemanticCache:
    def __init__(self, threshold: float = C.CACHE_SEMANTIC_THRESHOLD, clock=time.monotonic, rng=None):
        self.threshold = threshold
        self._clock = clock
        self._rng = rng or random.Random()
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _ttl(self) -> float:
        return C.CACHE_TTL_S + self._rng.uniform(-C.CACHE_TTL_JITTER_S, C.CACHE_TTL_JITTER_S)

    def get(self, tenant_id: str, user_id: str, query: str, qvec: np.ndarray):
        now = self._clock()
        with self._lock:
            for key, e in list(self._entries.items()):
                if e.expires_at <= now:
                    del self._entries[key]
            exact = self._entries.get(cache_key(tenant_id, user_id, query))
            best, best_sim = exact, 1.0 if exact else -1.0
            if exact is None:
                for e in self._entries.values():
                    if e.tenant_id != tenant_id or e.user_id != user_id:
                        continue  # tenant and user isolation: never compare across buckets
                    sim = float(np.dot(e.vector, qvec))
                    if sim > best_sim:
                        best, best_sim = e, sim
            if best is not None and best_sim >= self.threshold:
                self.hits += 1
                return {**best.payload, "cache_similarity": round(best_sim, 4), "cache_key": best.key}
            self.misses += 1
            return None

    def put(self, tenant_id: str, user_id: str, query: str, qvec: np.ndarray, payload: dict) -> str:
        key = cache_key(tenant_id, user_id, query)
        with self._lock:
            self._entries[key] = Entry(
                key=key,
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                vector=np.asarray(qvec, dtype="float32"),
                payload=payload,
                expires_at=self._clock() + self._ttl(),
            )
        return key

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(self.hits / total, 4) if total else 0.0,
        }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = 0


class RedisSemanticCache:
    """Same contract as SemanticCache, backed by Redis (design.md D5, D31).

    Keys: ua:cache:{tenant}:{user}:{sha}. Semantic matching scans only the caller's own prefix,
    so one tenant or user can never match another's entries. Redis TTL carries the jittered expiry.
    """

    def __init__(self, url: str, threshold: float = C.CACHE_SEMANTIC_THRESHOLD, rng=None):
        import redis

        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.threshold = threshold
        self._rng = rng or random.Random()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _prefix(tenant_id: str, user_id: str) -> str:
        return f"ua:cache:{tenant_id}:{user_id}:"

    def _ttl(self) -> int:
        return int(C.CACHE_TTL_S + self._rng.uniform(-C.CACHE_TTL_JITTER_S, C.CACHE_TTL_JITTER_S))

    def get(self, tenant_id: str, user_id: str, query: str, qvec: np.ndarray):
        prefix = self._prefix(tenant_id, user_id)
        exact_key = prefix + cache_key(tenant_id, user_id, query)
        raw = self.r.get(exact_key)
        if raw is not None:
            entry = json.loads(raw)
            self.hits += 1
            return {**entry["payload"], "cache_similarity": 1.0, "cache_key": exact_key}
        best, best_sim = None, -1.0
        for key in self.r.scan_iter(match=prefix + "*", count=500):
            entry = json.loads(self.r.get(key) or "null") or {}
            if not entry:
                continue
            sim = float(np.dot(np.array(entry["vector"], dtype="float32"), qvec))
            if sim > best_sim:
                best, best_sim = (key, entry), sim
        if best is not None and best_sim >= self.threshold:
            self.hits += 1
            return {**best[1]["payload"], "cache_similarity": round(best_sim, 4), "cache_key": best[0]}
        self.misses += 1
        return None

    def put(self, tenant_id: str, user_id: str, query: str, qvec: np.ndarray, payload: dict) -> str:
        key = self._prefix(tenant_id, user_id) + cache_key(tenant_id, user_id, query)
        body = {"query": query, "vector": [float(x) for x in qvec], "payload": payload}
        self.r.set(key, json.dumps(body), ex=self._ttl())
        return key

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"entries": int(self.r.dbsize()), "hits": self.hits, "misses": self.misses,
                "hit_ratio": round(self.hits / total, 4) if total else 0.0}

    def clear(self) -> None:
        for key in self.r.scan_iter(match="ua:cache:*", count=500):
            self.r.delete(key)
        self.hits = self.misses = 0


def make_cache():
    """Redis when REDIS_URL is set, otherwise the in-process cache."""
    import os

    url = os.environ.get("REDIS_URL")
    return RedisSemanticCache(url) if url else SemanticCache()
