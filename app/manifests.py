"""Signed MCP server manifests (brief Gate 13, design.md D32).

A manifest pins each server module by SHA-256 and lists its tools, then signs the whole record
with HMAC-SHA256. The gateway verifies every manifest before it starts a server, so a modified
server file or a forged manifest stops the server from starting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from app import config as C

KEY_PATH = C.ROOT / "data" / "keys" / "manifest.key"
MANIFEST_DIR = C.ROOT / "data" / "manifests"
SERVERS = {
    "rag": ("mcp_servers.rag_server", ["hybrid_retrieve", "chunk"]),
    "state": ("mcp_servers.state_server", ["session_facts_get", "session_fact_put", "approval_create",
                                           "approval_list", "approval_decide", "audit_list"]),
    "graph": ("mcp_servers.graphify_server", ["graph_subgraph"]),
}


class ManifestError(Exception):
    pass


def _key() -> bytes:
    env = os.environ.get("MANIFEST_SIGNING_KEY")
    if env:
        return env.encode("utf-8")
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_PATH.write_bytes(secrets.token_bytes(32))
    return KEY_PATH.read_bytes()


def _module_path(module: str) -> Path:
    return C.ROOT / (module.replace(".", "/") + ".py")


def _canonical(record: dict) -> bytes:
    body = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(body, sort_keys=True).encode("utf-8")


def sign_all() -> dict:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (module, tools) in SERVERS.items():
        digest = hashlib.sha256(_module_path(module).read_bytes()).hexdigest()
        record = {"server": name, "module": module, "sha256": digest, "tools": tools, "version": "0.1.0"}
        record["signature"] = hmac.new(_key(), _canonical(record), hashlib.sha256).hexdigest()
        (MANIFEST_DIR / f"{name}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        out[name] = record["sha256"][:12]
    return out


def verify(name: str) -> dict:
    """Raise ManifestError unless the manifest signature and the module hash both match."""
    path = MANIFEST_DIR / f"{name}.json"
    if not path.exists():
        raise ManifestError(f"no manifest for {name}; run scripts/sign_manifests.py")
    record = json.loads(path.read_text(encoding="utf-8"))
    expected = hmac.new(_key(), _canonical(record), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, record.get("signature", "")):
        raise ManifestError(f"{name}: manifest signature is invalid")
    module = record["module"]
    actual = hashlib.sha256(_module_path(module).read_bytes()).hexdigest()
    if not hmac.compare_digest(actual, record["sha256"]):
        raise ManifestError(f"{name}: server module changed since it was signed")
    return record
