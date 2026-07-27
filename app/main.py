# app/main.py
"""Public Health Evidence Assistant — query interface.

Runs the agent (query router: decompose multi-part questions, hybrid_rerank
retrieval, grounded generation) by default, with a sidebar selector to switch to
any single pipeline variant for comparison. Cited PMIDs are rendered as PubMed
links so grounded citations are verifiable in one click — the live answer to the
fabrication the no_retrieval baseline exhibits. Thumbs feedback and per-query
metadata are logged to Postgres for the monitoring dashboard. Each query is
traced to Langfuse. Sessions are capped to bound API cost.
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import re
import time
import uuid

import streamlit as st
from dotenv import load_dotenv

from agents.generate import MODES, generate
from agents.router import answer as agent_answer
from monitoring.store import log_query, record_feedback

load_dotenv()

SESSION_QUERY_CAP = 20
PMID_RE = re.compile(r"\[pmid:\s*(\d+)\]", re.IGNORECASE)
CMS_RE = re.compile(r"\[cms:\s*([a-z0-9-]+)\]", re.IGNORECASE)

# Optional Langfuse tracing. Absent keys degrade gracefully to no tracing.
try:
    from langfuse import Langfuse
    _lf = Langfuse() if os.environ.get("LANGFUSE_PUBLIC_KEY") else None
except Exception:
    _lf = None


# ---------------------------------------------------------------- corpus lookup


@st.cache_data(show_spinner=False)
def corpus_pmids() -> set[str]:
    """Corpus PMIDs, cached, for validating citations shown in the answer."""
    from ingestion.load import connect
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM documents")
        return {(r["doc_id"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()}


@st.cache_data(show_spinner=False)
def corpus_cms() -> dict[str, str]:
    """Map of CMS id (doc_id without the 'cms-' prefix) -> landing page url, for
    validating and linking CMS policy citations."""
    from ingestion.load import connect
    out: dict[str, str] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_id, url FROM documents WHERE source = 'cms'")
        for r in cur.fetchall():
            doc_id = r["doc_id"] if isinstance(r, dict) else r[0]
            url = r["url"] if isinstance(r, dict) else r[1]
            cms_id = doc_id[4:] if doc_id.startswith("cms-") else doc_id
            out[cms_id] = url or f"https://data.cms.gov/provider-data/dataset/{cms_id}"
    return out


def linkify_citations(answer_text: str, valid: set[str],
                      cms_urls: dict[str, str] | None = None) -> str:
    """Turn [PMID:12345] and [CMS:<id>] into markdown links, marking any that are
    not in the corpus."""
    cms_urls = cms_urls or {}

    def repl_pmid(m: re.Match) -> str:
        pmid = m.group(1)
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        if pmid in valid:
            return f"[[PMID:{pmid}]]({url})"
        return f"[[PMID:{pmid} — not in corpus]]({url})"

    def repl_cms(m: re.Match) -> str:
        cms_id = m.group(1)
        if cms_id in cms_urls:
            return f"[[CMS:{cms_id}]]({cms_urls[cms_id]})"
        return f"[CMS:{cms_id} — not in corpus]"

    linked = PMID_RE.sub(repl_pmid, answer_text)
    return CMS_RE.sub(repl_cms, linked)


# ---------------------------------------------------------------- session state


def init_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "n_queries" not in st.session_state:
        st.session_state.n_queries = 0
    if "history" not in st.session_state:
        st.session_state.history = []  # list of dicts: question, answer, query_id, ...


# ---------------------------------------------------------------- run one query


def run_query(question: str, mode: str) -> dict:
    valid = corpus_pmids()
    cms_urls = corpus_cms()
    t0 = time.time()

    def _count_cited(text: str) -> tuple[int, int]:
        """Count all citations (PMID + CMS) and how many resolve to the corpus."""
        pmids = PMID_RE.findall(text)
        cms_ids = CMS_RE.findall(text)
        n_cited = len(pmids) + len(cms_ids)
        n_valid = (sum(1 for p in pmids if p in valid)
                   + sum(1 for c in cms_ids if c in cms_urls))
        return n_cited, n_valid

    def _do_work():
        if mode == "agent_router":
            routed = agent_answer(question)
            return routed.generation, routed.multipart, routed.subquestions
        return generate(question, mode=mode), None, None

    if _lf:
        with _lf.start_as_current_observation(
            name="query", input={"question": question, "mode": mode}
        ) as obs:
            gen, multipart, subquestions = _do_work()
            n_cited, n_valid = _count_cited(gen.answer)
            obs.update(output={"answer": gen.answer, "n_cited": n_cited, "n_valid": n_valid})
    else:
        gen, multipart, subquestions = _do_work()
        n_cited, n_valid = _count_cited(gen.answer)

    latency_ms = int((time.time() - t0) * 1000)

    query_id = log_query(
        session_id=st.session_state.session_id,
        question=question,
        mode=mode,
        answer=gen.answer,
        context_pmids=gen.context_pmids,
        n_cited=n_cited,
        n_valid_cited=n_valid,
        prompt_tokens=gen.prompt_tokens,
        completion_tokens=gen.completion_tokens,
        latency_ms=latency_ms,
        multipart=multipart,
        subquestions=subquestions,
    )

    if _lf:
        _lf.flush()
    return {
        "question": question,
        "mode": mode,
        "answer": gen.answer,
        "answer_linked": linkify_citations(gen.answer, valid, cms_urls),
        "context_pmids": gen.context_pmids,
        "n_cited": n_cited,
        "n_valid": n_valid,
        "multipart": multipart,
        "subquestions": subquestions,
        "latency_ms": latency_ms,
        "query_id": query_id,
    }


# ---------------------------------------------------------------- UI


def main() -> None:
    st.set_page_config(page_title="Public Health Evidence Assistant", page_icon="🏥", layout="centered")
    init_state()

    st.title("Public Health Evidence Assistant")
    st.caption(
        "Answers on inpatient healthcare quality and safety, grounded in "
        "peer-reviewed PubMed abstracts. Every cited PMID links to the source."
    )

    with st.sidebar:
        st.subheader("Retrieval mode")
        mode_labels = {
            "agent_router": "Agent (decompose + hybrid + rerank)",
            "hybrid_rerank": "Hybrid + rerank",
            "hybrid_rrf": "Hybrid (RRF)",
            "dense_only": "Dense only",
            "lexical_only": "Lexical only",
            "no_retrieval": "No retrieval (baseline)",
        }
        choices = ["agent_router", *MODES]
        mode = st.radio(
            "How the answer is retrieved",
            choices,
            format_func=lambda m: mode_labels.get(m, m),
            index=0,
        )
        if mode == "no_retrieval":
            st.warning(
                "No-retrieval baseline answers from the model's own knowledge. "
                "Its citations are not grounded in the corpus and are typically "
                "invalid — this mode exists to show that contrast."
            )
        st.divider()
        remaining = SESSION_QUERY_CAP - st.session_state.n_queries
        st.metric("Queries left this session", max(remaining, 0))

    if st.session_state.n_queries >= SESSION_QUERY_CAP:
        st.info(
            "You've reached this session's query limit. Refresh to start a new "
            "session. The limit keeps API usage bounded for this demo."
        )
        _render_history()
        return

    question = st.chat_input("Ask about readmissions, safety, quality, infections, or mortality")
    if question:
        st.session_state.n_queries += 1
        with st.spinner("Retrieving evidence and composing an answer…"):
            result = run_query(question, mode)
        st.session_state.history.insert(0, result)

    _render_history()


def _render_history() -> None:
    for i, r in enumerate(st.session_state.history):
        with st.container(border=True):
            st.markdown(f"**Q:** {r['question']}")
            if r.get("multipart"):
                st.caption("Detected as multi-part. Retrieved for each sub-question:")
                for s in r["subquestions"]:
                    st.caption(f"• {s}")
            st.markdown(r["answer_linked"])

            cols = st.columns([1, 1, 6])
            key_base = f"{r['query_id']}"
            if cols[0].button("👍", key=f"up_{key_base}"):
                record_feedback(r["query_id"], st.session_state.session_id, 1)
                st.toast("Thanks — recorded 👍")
            if cols[1].button("👎", key=f"down_{key_base}"):
                record_feedback(r["query_id"], st.session_state.session_id, -1)
                st.toast("Thanks — recorded 👎")

            valid_note = (
                f"{r['n_valid']}/{r['n_cited']} citations in corpus"
                if r["n_cited"] else "no citations"
            )
            cols[2].caption(
                f"{r['mode']} · {valid_note} · {r['latency_ms']} ms"
            )


if __name__ == "__main__":
    main()