import numpy as np

from app.cache.semantic import SemanticCache, cache_key


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def unit(v):
    v = np.asarray(v, dtype="float32")
    return v / np.linalg.norm(v)


def test_exact_hit_same_tenant_and_user():
    c = SemanticCache(clock=Clock())
    c.put("tenant_a", "u1", "Lot release window?", unit([1, 0]), {"answer": "48h"})
    hit = c.get("tenant_a", "u1", "  lot RELEASE window? ", unit([0, 1]))
    assert hit and hit["answer"] == "48h"


def test_no_cross_tenant_leak_even_with_identical_embedding():
    c = SemanticCache(clock=Clock())
    c.put("tenant_a", "u1", "Lot release window?", unit([1, 0]), {"answer": "secret-a"})
    assert c.get("tenant_b", "u1", "Lot release window?", unit([1, 0])) is None
    assert c.get("tenant_b", "u1", "a totally different question", unit([1, 0])) is None


def test_no_cross_user_leak_within_tenant():
    c = SemanticCache(clock=Clock())
    c.put("tenant_a", "u1", "Lot release window?", unit([1, 0]), {"answer": "x"})
    assert c.get("tenant_a", "u2", "Lot release window?", unit([1, 0])) is None


def test_semantic_threshold_is_enforced():
    c = SemanticCache(clock=Clock(), threshold=0.92)
    c.put("tenant_a", "u1", "q1", unit([1, 0]), {"answer": "a"})
    close = unit([1, 0.3])  # cosine about 0.958
    far = unit([1, 1])  # cosine about 0.707
    assert c.get("tenant_a", "u1", "q2", close) is not None
    assert c.get("tenant_a", "u1", "q3", far) is None


def test_ttl_expiry_and_jitter_bounds():
    clk = Clock()
    c = SemanticCache(clock=clk)
    c.put("tenant_a", "u1", "q", unit([1, 0]), {"answer": "a"})
    entry = next(iter(c._entries.values()))
    assert 300 - 45 <= entry.expires_at <= 300 + 45
    clk.t = 400
    assert c.get("tenant_a", "u1", "q", unit([1, 0])) is None


def test_cache_key_is_tenant_user_scoped_sha256():
    k1 = cache_key("tenant_a", "u1", "Q?")
    assert len(k1) == 64 and k1 != cache_key("tenant_b", "u1", "Q?") and k1 != cache_key("tenant_a", "u2", "Q?")
