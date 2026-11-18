"""Index build: vault notes -> chunks -> BM25 tokens + dense FAISS HNSW index (design.md §6, D6)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import yaml

from app.config import CHUNK_OVERLAP, CHUNK_TOKENS, EMBED_MODEL, HNSW_EF_SEARCH, HNSW_M

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_\-]+)\]\]")


@dataclass
class Chunk:
    chunk_id: str
    chunk_hash: str
    doc_id: str
    title: str
    domain: str
    risk_tier: int
    tenants: list[str]
    text: str
    links: list[str]


def parse_note(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    meta = yaml.safe_load(match.group(1))
    for key in ("id", "domain", "risk_tier", "version", "tenants"):
        if key not in meta:
            raise ValueError(f"{path.name}: frontmatter missing '{key}'")
    return meta, match.group(2).strip()


def clean_body(body: str) -> str:
    """Drop Markdown heading lines (the title is stored separately) and bullet markers."""
    lines = [re.sub(r"^\s*-\s+", "", ln) for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    text = "\n".join(lines).strip()
    return WIKILINK_RE.sub(r"\1", text)  # [[POL-LOT-002]] -> POL-LOT-002 in display text


def chunk_words(words: list[str], size: int = CHUNK_TOKENS, overlap: int = CHUNK_OVERLAP):
    """Sliding windows over words. Tokens are approximated as words (documented in design.md)."""
    if len(words) <= size:
        return [words]
    step = size - overlap
    return [words[i : i + size] for i in range(0, max(1, len(words) - overlap), step)]


def load_chunks(vault: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(vault.glob("*.md")):
        meta, body = parse_note(path)
        words = clean_body(body).split()
        for n, window in enumerate(chunk_words(words)):
            text = " ".join(window)
            chunks.append(
                Chunk(
                    chunk_id=f"{meta['id']}#c{n}",
                    chunk_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                    doc_id=meta["id"],
                    title=path.stem.replace("-", " "),
                    domain=meta["domain"],
                    risk_tier=int(meta["risk_tier"]),
                    tenants=[str(t) for t in meta["tenants"]],
                    text=text,
                    links=WIKILINK_RE.findall(body),
                )
            )
    return chunks


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def embed(texts: list[str]) -> np.ndarray:
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=EMBED_MODEL)
    return _normalize(np.array(list(model.embed(texts)), dtype="float32"))


def build(vault: Path, index_dir: Path) -> dict:
    import faiss

    index_dir.mkdir(parents=True, exist_ok=True)
    chunks = load_chunks(vault)
    vectors = embed([c.text for c in chunks])
    dim = vectors.shape[1]
    index = faiss.IndexHNSWFlat(dim, HNSW_M, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efSearch = HNSW_EF_SEARCH
    index.add(vectors)
    faiss.write_index(index, str(index_dir / "dense.faiss"))
    np.save(index_dir / "vectors.npy", vectors)
    (index_dir / "chunks.json").write_text(
        json.dumps([asdict(c) for c in chunks], indent=2), encoding="utf-8"
    )
    return {"chunks": len(chunks), "dim": dim, "index_dir": str(index_dir)}


def load_index(index_dir: Path) -> tuple[list[Chunk], np.ndarray, object]:
    import faiss

    raw = json.loads((index_dir / "chunks.json").read_text(encoding="utf-8"))
    chunks = [Chunk(**c) for c in raw]
    vectors = np.load(index_dir / "vectors.npy")
    dense = faiss.read_index(str(index_dir / "dense.faiss"))
    dense.hnsw.efSearch = HNSW_EF_SEARCH
    return chunks, vectors, dense
