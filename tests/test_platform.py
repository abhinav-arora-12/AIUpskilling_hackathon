import asyncio
import os
import shutil
import uuid

import numpy as np
import pytest

from app import manifests
from app.agent.subagents import ExecutionSubagent, RetrievalSubagent, VerificationSubagent
from app.state_store import StateStore


async def _recorder(server, tool, args):
    return {"server": server, "tool": tool}


def run(coro):
    return asyncio.run(coro)


# ---- subagents: enforced tool scopes --------------------------------------------------------
def test_retrieval_subagent_cannot_mutate_state():
    sub = RetrievalSubagent(_recorder)
    assert run(sub.invoke("rag", "hybrid_retrieve", {}))["tool"] == "hybrid_retrieve"
    with pytest.raises(PermissionError):
        run(sub.invoke("state", "approval_create", {}))


def test_execution_subagent_can_request_but_never_decide():
    sub = ExecutionSubagent(_recorder)
    assert run(sub.invoke("state", "approval_create", {}))["tool"] == "approval_create"
    with pytest.raises(PermissionError):
        run(sub.invoke("state", "approval_decide", {}))  # reviewers only, never an agent


def test_verification_subagent_has_no_retrieval_access():
    sub = VerificationSubagent(_recorder)
    with pytest.raises(PermissionError):
        run(sub.invoke("rag", "hybrid_retrieve", {}))


# ---- signed manifests ----------------------------------------------------------------------
def test_manifests_verify_when_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(manifests, "MANIFEST_DIR", tmp_path / "m")
    monkeypatch.setattr(manifests, "KEY_PATH", tmp_path / "k.bin")
    manifests.sign_all()
    for name in ("rag", "state", "graph"):
        assert manifests.verify(name)["server"] == name


def test_forged_manifest_is_rejected(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(manifests, "MANIFEST_DIR", tmp_path / "m")
    monkeypatch.setattr(manifests, "KEY_PATH", tmp_path / "k.bin")
    manifests.sign_all()
    path = tmp_path / "m" / "rag.json"
    rec = json.loads(path.read_text())
    rec["tools"].append("approval_decide")  # tamper with the record
    path.write_text(json.dumps(rec))
    with pytest.raises(manifests.ManifestError):
        manifests.verify("rag")


def test_changed_server_module_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(manifests, "MANIFEST_DIR", tmp_path / "m")
    monkeypatch.setattr(manifests, "KEY_PATH", tmp_path / "k.bin")
    manifests.sign_all()
    original = manifests._module_path("mcp_servers.graphify_server")
    backup = tmp_path / "backup.py"
    shutil.copy(original, backup)
    try:
        original.write_text(original.read_text() + "\n# tampered\n")
        with pytest.raises(manifests.ManifestError):
            manifests.verify("graph")
    finally:
        shutil.copy(backup, original)  # always restore the real file


# ---- bulkheads -------------------------------------------------------------------------------
def test_bulkhead_caps_concurrency_per_server():
    from app.agent.gateway import MCPHub

    hub = MCPHub.__new__(MCPHub)
    hub.sessions = {}
    hub._bulkheads = {"rag": asyncio.Semaphore(2)}
    active, peak = 0, 0

    class FakeSession:
        async def call_tool(self, tool, args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

            class R:
                content = []
            return R()

    hub.sessions = {"rag": FakeSession()}

    async def burst():
        await asyncio.gather(*[hub.call("rag", "hybrid_retrieve", {}) for _ in range(10)])

    run(burst())
    assert peak == 2  # never more than the pool size in flight


# ---- state store backends ------------------------------------------------------------------
def _exercise(store):
    row = store.approval_create("tenant_a", "operator-b", "lot.release", {"lot_id": "LOT-00044"}, str(uuid.uuid4()))
    assert row["status"] == "PENDING"
    store.audit_append("tenant_a", "BACKEND_TEST", {"n": 1})
    assert store.audit_verify("tenant_a")
    fid = store.fact_put("tenant_a", "sess-b", "k", "v", "POL-LOT-001#c0")
    assert isinstance(fid, int)
    assert store.facts_get("tenant_a", "sess-b")[0]["value"] == "v"
    assert store.facts_get("tenant_b", "sess-b") == []  # tenant scoped


def test_sqlite_backend(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3", dsn="")
    assert store.dialect == "sqlite"
    _exercise(store)


@pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"), reason="POSTGRES_DSN not set")
def test_postgres_backend():
    store = StateStore(dsn=os.environ["POSTGRES_DSN"])
    assert store.dialect == "postgres"
    _exercise(store)
    store.close()


@pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="REDIS_URL not set")
def test_redis_cache_isolation():
    from app.cache.semantic import RedisSemanticCache

    c = RedisSemanticCache(os.environ["REDIS_URL"])
    c.clear()
    v = np.array([1, 0], dtype="float32")
    c.put("tenant_a", "u1", "Lot window?", v, {"answer": "48h"})
    assert c.get("tenant_a", "u1", "Lot window?", v) is not None
    assert c.get("tenant_b", "u1", "Lot window?", v) is None
    assert c.get("tenant_a", "u2", "Lot window?", v) is None
    c.clear()
