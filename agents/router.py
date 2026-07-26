# agents/router.py
"""Agentic query router: decompose multi-part questions before retrieval.

Motivation, measured: dense retrieval collapses multi-part questions onto a
single topic. q14 ("which discharge interventions reduce both readmissions and
post-discharge mortality") retrieves 10/10 readmissions documents and never
touches mortality. The router adds an LLM decision node that detects multi-part
questions, splits them into sub-questions, retrieves for each, and merges the
results so the generator sees evidence from every part.

This is the one genuinely agentic component: the route node makes a control-flow
decision (answer directly vs decompose-and-merge) rather than running a fixed
sequence. Built on LangGraph so the branch is explicit and traceable.

For single-part questions the router is a pass-through to the same
hybrid_rerank + grounded generation path as agents.generate.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from agents.generate import GenerationResult, generate
from retrieval.rerank import search_hybrid_rerank
from retrieval.search import Hit

load_dotenv()

ROUTER_MODEL = "gpt-4o-mini"  # routing is cheap classification; generation stays gpt-4o
DEFAULT_TOP_K = 5
MAX_SUBQUESTIONS = 4

_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


ROUTER_SYSTEM = """You decide how to retrieve evidence for a question about \
inpatient healthcare quality and safety.

Some questions ask about a single topic. Others cover two distinct topics that \
live in different parts of the medical literature, so retrieving for the whole \
question at once tends to return documents about only one of them. A question is \
multi-part when it spans two different subject areas, whether that is stated as:
- an explicit conjunction — "interventions that reduce BOTH readmissions AND \
mortality"; or
- a relationship between two topics — "how do infections affect mortality", \
"do hospitals with strong quality indicators have lower mortality". Here the two \
topics are the two things being related (infections and mortality; quality and \
mortality), and each has its own body of evidence.

A question is NOT multi-part just because answering it draws on background from \
several areas. "What was the impact of a readmissions penalty program" is one \
question about one policy, even though the evidence touches both readmissions and \
quality improvement. Split only when the question genuinely asks about two \
distinct subjects.

Decide whether the question is single-part or multi-part.
- If single-part, return the question unchanged as the only sub-question.
- If multi-part, split it into 2 to 4 self-contained sub-questions, each \
retrievable on its own, together covering every part of the original. For a \
relationship question, one sub-question per topic (e.g. "how do healthcare-\
associated infections affect patient outcomes" and "what drives hospital \
mortality") so each topic's literature is retrieved.

Respond with a JSON object:
{"multipart": <true|false>, "subquestions": ["...", "..."]}

Each sub-question must be a complete, standalone question, not a fragment."""


@dataclass
class RoutedResult:
    """Wraps a GenerationResult with the routing decision that produced it."""
    generation: GenerationResult
    multipart: bool
    subquestions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- routing call


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(6),
)
def _route_call(question: str) -> dict:
    resp = _client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": question},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        obj = json.loads(raw)
        multipart = bool(obj.get("multipart", False))
        subs = obj.get("subquestions") or []
        subs = [s.strip() for s in subs if isinstance(s, str) and s.strip()]
        subs = subs[:MAX_SUBQUESTIONS]
        if not subs:
            subs = [question]
        # A "multipart" decision with only one sub-question is effectively single.
        if len(subs) < 2:
            multipart = False
        return {"multipart": multipart, "subquestions": subs}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"multipart": False, "subquestions": [question]}


# ---------------------------------------------------------------- merge


def _merge_hits(hit_lists: list[list[Hit]], k: int) -> list[Hit]:
    """Union sub-question retrievals, deduplicating by chunk_id.

    A document retrieved for several sub-questions is kept once, at its best
    (lowest) rank across the lists. Documents are then interleaved by rank so
    that each sub-question's top results are represented, rather than letting
    one sub-question's list dominate the head.
    """
    best_rank: dict[int, int] = {}
    exemplar: dict[int, Hit] = {}
    for hits in hit_lists:
        for rank, h in enumerate(hits):
            if h.chunk_id not in best_rank or rank < best_rank[h.chunk_id]:
                best_rank[h.chunk_id] = rank
            exemplar.setdefault(h.chunk_id, h)
    ordered = sorted(best_rank, key=lambda cid: (best_rank[cid], cid))
    return [exemplar[cid] for cid in ordered[:k]]


# ---------------------------------------------------------------- graph state


class RouterState(TypedDict, total=False):
    question: str
    k: int
    multipart: bool
    subquestions: list[str]
    contexts: list[Hit]
    result: GenerationResult


def _node_route(state: RouterState) -> RouterState:
    decision = _route_call(state["question"])
    state["multipart"] = decision["multipart"]
    state["subquestions"] = decision["subquestions"]
    return state


def _node_retrieve(state: RouterState) -> RouterState:
    k = state.get("k", DEFAULT_TOP_K)
    subs = state["subquestions"]
    if len(subs) <= 1:
        state["contexts"] = search_hybrid_rerank(subs[0], k=k)
    else:
        # Retrieve per sub-question, then merge. Each sub-question gets the full
        # k so the merge has enough candidates from every part to interleave.
        hit_lists = [search_hybrid_rerank(s, k=k) for s in subs]
        state["contexts"] = _merge_hits(hit_lists, k=k)
    return state


def _node_generate(state: RouterState) -> RouterState:
    # Reuse the grounded generation path over the router's merged context.
    # Passing contexts= skips retrieval inside generate() so the router stays in
    # control of what was retrieved, while the prompt and generation logic live
    # in one place.
    result = generate(
        state["question"],
        mode="hybrid_rerank",
        contexts=state["contexts"],
    )
    result.mode = "agent_router"
    state["result"] = result
    return state


def _build_graph():
    g = StateGraph(RouterState)
    g.add_node("route", _node_route)
    g.add_node("retrieve", _node_retrieve)
    g.add_node("generate", _node_generate)
    g.set_entry_point("route")
    g.add_edge("route", "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()


_GRAPH = _build_graph()


# ---------------------------------------------------------------- public entry point


def answer(question: str, k: int = DEFAULT_TOP_K) -> RoutedResult:
    """Route, retrieve (decomposed if multi-part), and generate."""
    final = _GRAPH.invoke({"question": question, "k": k})
    return RoutedResult(
        generation=final["result"],
        multipart=final["multipart"],
        subquestions=final["subquestions"],
    )


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or (
        "Which discharge interventions reduce both readmissions and "
        "post-discharge mortality?"
    )
    r = answer(q)
    print(f"multipart: {r.multipart}")
    print(f"subquestions: {r.subquestions}")
    print(f"context: {r.generation.context_pmids}")
    print(f"\n{r.generation.answer}")