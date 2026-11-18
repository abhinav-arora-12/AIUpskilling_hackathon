"""RAGAS evaluation of the golden run (brief Gate 8, design.md D12, D34).

Judge LLM: anthropic/claude-haiku-5.5 through OpenRouter (cheap, same account).
Embeddings: BAAI/bge-large-en-v1.5 via fastembed (same model as retrieval).
Input: answered golden items from data/results/golden_run.json.
Output: data/results/ragas_run.json

Usage: uv run python scripts/run_ragas.py
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.embeddings import Embeddings  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from ragas import EvaluationDataset, evaluate  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import (  # noqa: E402
    AnswerRelevancy,
    ContextEntityRecall,
    Faithfulness,
    LLMContextPrecisionWithReference,
    LLMContextRecall,
)

from app import config as C  # noqa: E402
from app.config import PATHS  # noqa: E402
from app.rag.index import embed as embed_docs  # noqa: E402
from app.rag.retriever import embed_query  # noqa: E402


class BgeEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return embed_docs(list(texts)).tolist()

    def embed_query(self, text):
        return embed_query(text).tolist()


def judge_llm():
    if not C.SETTINGS.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY not set; RAGAS needs a judge model")
    chat = ChatOpenAI(model=C.OPENROUTER_TRIAGE_MODEL, base_url="https://openrouter.ai/api/v1",
                      api_key=C.SETTINGS.openrouter_api_key, temperature=0.0, max_retries=2, timeout=60)
    return LangchainLLMWrapper(chat)


def main() -> None:
    run = json.loads((PATHS.results / "golden_run.json").read_text(encoding="utf-8"))
    by_id = {json.loads(x)["item_id"]: json.loads(x)
             for x in PATHS.golden.read_text(encoding="utf-8").splitlines()}
    chunks = {c["chunk_id"]: c for c in json.loads((PATHS.index_dir / "chunks.json").read_text(encoding="utf-8"))}
    rows, meta = [], []
    for r in run["records"]:
        if r["status"] != "ANSWERED" or not r["answer"]:
            continue
        contexts = [chunks[c]["text"] for c in r["top4_ids"] if c in chunks]
        rows.append({
            "user_input": by_id[r["item_id"]]["question"],
            "response": r["answer"],
            "retrieved_contexts": contexts,
            "reference": r["reference_answer"],
        })
        meta.append(r["item_id"])
    if not rows:
        raise SystemExit("no answered items in golden_run.json")
    llm = judge_llm()
    emb = LangchainEmbeddingsWrapper(BgeEmbeddings())
    metrics = [Faithfulness(llm=llm), AnswerRelevancy(llm=llm, embeddings=emb),
               LLMContextPrecisionWithReference(llm=llm), LLMContextRecall(llm=llm),
               ContextEntityRecall(llm=llm)]
    t0 = time.time()
    from ragas.run_config import RunConfig

    # Judge calls are network-bound, so more parallel workers shorten the run without changing results.
    run_cfg = RunConfig(max_workers=32, timeout=120, max_retries=3)
    result = evaluate(EvaluationDataset.from_list(rows), metrics=metrics, raise_exceptions=False,
                      show_progress=False, run_config=run_cfg)
    df = result.to_pandas()
    per_item = []
    for i, item_id in enumerate(meta):
        rec = {"item_id": item_id}
        for col in ("faithfulness", "answer_relevancy", "llm_context_precision_with_reference",
                    "context_recall", "context_entity_recall"):
            if col in df.columns:
                v = df.iloc[i][col]
                rec[col] = None if v != v else round(float(v), 4)  # NaN check
        per_item.append(rec)

    def mean(key):
        vals = [p[key] for p in per_item if p.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "judge": f"openrouter:{C.OPENROUTER_TRIAGE_MODEL}",
        "embeddings": C.EMBED_MODEL,
        "n": len(per_item),
        "seconds": round(time.time() - t0, 1),
        "means": {k: mean(k) for k in ("faithfulness", "answer_relevancy",
                                       "llm_context_precision_with_reference", "context_recall",
                                       "context_entity_recall")},
        "per_item": per_item,
    }
    (PATHS.results / "ragas_run.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("n", "means", "seconds")}, indent=2))


if __name__ == "__main__":
    main()
