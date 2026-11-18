"""Streamlit developer workbench (design.md D-table, §9, brief Gate 18). Six tabs.

Run the API first:  uv run uvicorn app.main:app --port 8000
Then:               uv run streamlit run app.py
"""

from __future__ import annotations

import json
import uuid

import httpx
import pandas as pd
import streamlit as st

from app import config as C
from app.graph import load_graph, subgraph
from app.plugins.hooks import REGISTRY

st.set_page_config(page_title="The Unreliable Assistant: Workbench", layout="wide")

API = st.sidebar.text_input("API base URL", "http://127.0.0.1:8000")
TENANT = st.sidebar.selectbox("Active tenant", ["tenant_a", "tenant_b"], index=0)
USER = st.sidebar.text_input("User id", "operator-ui")
ROOT = C.ROOT
RESULTS = C.PATHS.results


def api_get(path: str, **params):
    try:
        r = httpx.get(f"{API}{path}", params=params, timeout=30)
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": "API_UNREACHABLE", "message": str(exc)}


def api_post(path: str, body: dict, headers: dict | None = None):
    try:
        r = httpx.post(f"{API}{path}", json=body, headers=headers or {}, timeout=120)
        return r.status_code, r.json()
    except httpx.HTTPError as exc:
        return 0, {"error": "API_UNREACHABLE", "message": str(exc)}


def status_badge(status: str) -> str:
    colors = {"ANSWERED": "green", "ABSTAINED": "orange", "PENDING_APPROVAL": "blue", "BLOCKED": "red",
              "REJECTED_OOS": "gray", "HALTED_BREAKER": "red", "ERROR": "red"}
    return f":{colors.get(status, 'gray')}[**{status}**]"


tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "1. Architecture & Workflow",
    "2. 3-Stage RAG Lab",
    "3. MCP & Plugin Inspector",
    "4. Memory & Graphify Tokens",
    "5. Red Team & Circuit Breaker",
    "6. 36-KPI Scorecard & Load",
])

# ---- Tab 1: architecture, query console, ReAct trace, HITL queue ---------------------------
with tab1:
    st.subheader("Gate 1: design.md")
    design = (ROOT / "design.md").read_text(encoding="utf-8") if (ROOT / "design.md").exists() else ""
    locked = "LOCKED" in design.split("## Assumptions")[0]
    st.write(f"Status: {'**LOCKED**' if locked else 'not locked'}. Decisions D1 to D20 are listed in design.md.")
    left, right = st.columns([1, 1])
    with left:
        if C.PATHS.png.exists():
            st.image(str(C.PATHS.png), caption="Gate 2: workflow_orchestration.png (300 DPI)")
        else:
            st.warning("docs/workflow_orchestration.png not found")
    with right:
        st.markdown("#### Live query console")
        query = st.text_area("Question", "What is the QA sign-off window for releasing a lot?", height=80)
        key = st.session_state.setdefault("idem", str(uuid.uuid4()))
        if st.button("Send", type="primary"):
            code, body = api_post(
                "/api/v1/agent/query",
                {"tenant_id": TENANT, "user_id": USER, "query": query},
                {"X-Tenant-ID": TENANT, "X-Idempotency-Key": key},
            )
            st.session_state["last"] = body
            st.session_state["idem"] = str(uuid.uuid4())  # a new key per request
        last = st.session_state.get("last")
        if last:
            st.markdown(f"{status_badge(last.get('status', '?'))}  route **{last.get('route')}**  "
                        f"latency **{last.get('latency_ms')} ms**  trace `{last.get('trace_id')}`")
            st.write(last.get("answer") or last.get("message") or last)
            for c in last.get("citations", []):
                st.caption(f"{c['chunk_id']}  rerank {c.get('rerank_score')}")
            with st.expander("ReAct step trace"):
                st.json(last.get("loop", {}))
            with st.expander("Full response"):
                st.json(last)

    st.markdown("#### Tier 3 HITL queue")
    pend = api_get("/api/v1/hitl/pending", tenant_id=TENANT)
    approvals = pend.get("approvals", []) if isinstance(pend, dict) else []
    if not approvals:
        st.caption("No pending approvals for this tenant.")
    for a in approvals:
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(f"`{a['approval_id']}`  **{a['action']}**  args {a['args_json']}  by {a['requested_by']}")
        reviewer = c2.selectbox("Reviewer", ["reviewer-1", "reviewer-2"], key=f"rv-{a['approval_id']}")
        if c3.button("Approve", key=f"ap-{a['approval_id']}"):
            code, body = api_post(f"/api/v1/hitl/{a['approval_id']}/decision",
                                  {"tenant_id": TENANT, "decision": "APPROVE", "reviewer_id": reviewer},
                                  {"X-Tenant-ID": TENANT})
            st.write(body)
            st.rerun()
        if st.button("Reject", key=f"rj-{a['approval_id']}"):
            code, body = api_post(f"/api/v1/hitl/{a['approval_id']}/decision",
                                  {"tenant_id": TENANT, "decision": "REJECT", "reviewer_id": reviewer},
                                  {"X-Tenant-ID": TENANT})
            st.write(body)
            st.rerun()

# ---- Tab 2: 3-stage RAG lab ----------------------------------------------------------------
with tab2:
    st.subheader("Hybrid retrieval, stage by stage")
    st.caption("Runs the same retriever code as mcp-hybrid-rag, in this process.")
    q2 = st.text_input("Query", "What is the QA sign-off window for releasing a lot?", key="rag_q")
    c1, c2, c3, c4 = st.columns(4)
    k1 = c1.slider("BM25 k1", 0.0, 3.0, C.BM25_K1, 0.05)
    b = c2.slider("BM25 b", 0.0, 1.0, C.BM25_B, 0.05)
    rrf_k = c3.slider("RRF k", 1, 200, C.RRF_K, 1)
    thr = c4.slider("Abstain threshold", 0.0, 1.0, C.ABSTAIN_THRESHOLD, 0.01)
    if st.button("Run retrieval"):
        from app.rag.retriever import get_retriever

        with st.spinner("Running 3 stages (first run loads models)"):
            out = get_retriever().retrieve(q2, TENANT, k1=k1, b=b, rrf_k=rrf_k, threshold=thr)
        st.session_state["rag_out"] = out
    out = st.session_state.get("rag_out")
    if out:
        m1, m2, m3 = st.columns(3)
        m1.metric("Top rerank score", f"{out['top_score']:.4f}")
        m2.metric("Abstain", str(out["abstain"]))
        m3.metric("Timings (ms)", json.dumps(out["timings_ms"]))
        rows = [{
            "chunk_id": c["chunk_id"], "stage1_bm25": c["bm25"], "stage2_cosine": c["cosine"],
            "rrf_rank": c["rrf_rank"], "rrf_score": c["rrf_score"], "stage3_rerank": c["rerank_score"],
        } for c in out["all_candidates"]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.caption(f"Stage counts {out['stage_counts']}. Tenant {TENANT} sees only entitled chunks.")

# ---- Tab 3: MCP and plugin inspector ------------------------------------------------------
with tab3:
    st.subheader("MCP servers")
    health = api_get("/api/v1/health")
    if "mcp" in health:
        st.json(health)
    else:
        st.error(f"API not reachable: {health}")
    st.subheader("Lifecycle plugins (read-only in this build, design.md D14)")
    st.dataframe(pd.DataFrame([{
        "name": h.name, "stage": h.stage, "priority": h.priority, "fail_closed": h.fail_closed,
        "enabled": True} for h in REGISTRY]), use_container_width=True)
    st.subheader("Recent MCP spans (JSON-RPC timings reported by servers)")
    spans = api_get("/api/v1/traces", limit=200).get("spans", [])
    mcp_spans = [s for s in spans if s["name"].startswith("mcp.")]
    st.dataframe(pd.DataFrame([{"span": s["name"], "ms": s["duration_ms"], "attrs": json.dumps(s["attrs"])[:120]}
                               for s in mcp_spans[-50:]]), use_container_width=True)

# ---- Tab 4: memory and graphify tokens ----------------------------------------------------
with tab4:
    st.subheader("Graphify subgraph (mcp-graphify-vault, depth <= 2)")
    anchor = st.selectbox("Anchor note", sorted(n["id"] for n in load_graph()["nodes"]))
    depth = st.radio("Depth", [1, 2], horizontal=True)
    sg = subgraph(anchor, depth, TENANT)
    st.write(f"{len(sg.get('nodes', []))} nodes, {len(sg.get('edges', []))} edges, "
             f"~{sg.get('token_estimate', 0)} words")
    st.json(sg)
    st.subheader("Atomic note viewer")
    note = st.selectbox("Note", sorted(p.name for p in C.PATHS.vault.glob("*.md")))
    st.code((C.PATHS.vault / note).read_text(encoding="utf-8"), language="markdown")
    st.subheader("Session facts (sqlite state store)")
    session = st.text_input("Session id", f"s-{USER}")
    st.caption("Facts are written by the gateway after verified answers.")
    from app.state_store import StateStore

    facts = StateStore().facts_get(TENANT, session)
    st.dataframe(pd.DataFrame(facts), use_container_width=True) if facts else st.caption("No facts for this session.")
    st.subheader("Token savings")
    kpis = json.loads((RESULTS / "kpis.json").read_text()) if (RESULTS / "kpis.json").exists() else {}
    t26 = next((k for k in kpis.get("kpis", []) if k["id"] == "KPI-26"), None)
    if t26 and isinstance(t26.get("value"), (int, float)):
        st.metric("Prompt token reduction vs raw dump (words proxy)", f"{t26['value']:.1f}%",
                  help=t26.get("method"))
    else:
        st.caption("Run scripts/scorecard.py to compute token savings.")

# ---- Tab 5: red team and circuit breaker --------------------------------------------------
with tab5:
    st.subheader("Red-team suite (100 synthetic prompts)")
    rt_path = RESULTS / "redteam_run.json"
    if rt_path.exists():
        rt = json.loads(rt_path.read_text())
        st.metric("Passed", f"{rt['passed']} / {rt['total']}")
        st.dataframe(pd.DataFrame([{"category": k, **v} for k, v in rt["by_category"].items()]),
                     use_container_width=True)
    else:
        st.info("Run scripts/run_redteam.py to produce results.")
    st.subheader("Circuit breaker")
    h = api_get("/api/v1/health")
    st.write(f"Open breaker keys: **{h.get('breaker_open_keys', 'n/a')}**")
    if st.button("Reset breaker"):
        st.write(httpx.post(f"{API}/api/v1/breaker/reset", timeout=10).json())

# ---- Tab 6: scorecard and load ---------------------------------------------------------------
with tab6:
    st.subheader("36-KPI scorecard")
    kpis = json.loads((RESULTS / "kpis.json").read_text()) if (RESULTS / "kpis.json").exists() else None
    if not kpis:
        st.info("Run scripts/scorecard.py to build the scorecard.")
    else:
        df = pd.DataFrame(kpis["kpis"])
        counts = df["status"].value_counts().to_dict()
        st.write(f"PASS {counts.get('PASS', 0)} | FAIL {counts.get('FAIL', 0)} | "
                 f"NOT_MEASURED {counts.get('NOT_MEASURED', 0)}")
        st.dataframe(df[["id", "stage", "name", "target", "value", "status", "method"]],
                     use_container_width=True, hide_index=True)
    gp = RESULTS / "golden_run.json"
    if gp.exists():
        g = json.loads(gp.read_text())
        lat = pd.DataFrame([{"stratum": r["stratum"], "wall_ms": r["wall_ms"]} for r in g["records"]])
        st.subheader("End-to-end latency, golden run (ms)")
        st.bar_chart(lat.groupby("stratum")["wall_ms"].median())
    st.subheader("Span waterfall (latest trace)")
    spans = api_get("/api/v1/traces", limit=300).get("spans", [])
    if spans:
        tid = spans[-1]["trace_id"]
        rows = [s for s in spans if s["trace_id"] == tid]
        st.dataframe(pd.DataFrame([{"span": s["name"], "ms": s["duration_ms"]} for s in rows]),
                     use_container_width=True)
    load = RESULTS / "load_run.json"
    if load.exists():
        st.subheader("Scaled load run (k6)")
        st.json(json.loads(load.read_text()))
