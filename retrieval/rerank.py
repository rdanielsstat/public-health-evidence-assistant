"""Cross-encoder reranking over hybrid candidates.

Reranking takes a candidate set produced by a first-stage retriever (here,
hybrid RRF) and reorders it with a cross-encoder that scores each
(query, document) pair jointly, rather than relying on the separate lexical
and dense scores that produced the candidate ranking.

Model: BAAI/bge-reranker-v2-m3, run locally via sentence-transformers.
No API key, no network at query time once the model is cached.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

from retrieval.fusion import search_hybrid
from retrieval.search import Hit, search_dense, search_lexical

# One place to change the model. bge-reranker-base is the older, lighter
# option if v2-m3's download (~2.3GB) is too heavy for your setup.
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

# How many first-stage candidates to rerank. The reranker only reorders what
# it is given, so this caps recall: a relevant document outside the top
# CANDIDATE_K is unreachable no matter how good the reranker is.
CANDIDATE_K = 30


@lru_cache(maxsize=1)
def _load_model():
    """Load and cache the cross-encoder. Imported lazily so that importing
    this module does not pull in torch unless reranking is actually used."""
    from sentence_transformers import CrossEncoder

    return CrossEncoder(RERANKER_MODEL)


def rerank(query: str, hits: list[Hit], k: int = 10) -> list[Hit]:
    """Reorder hits by cross-encoder relevance to the query.

    The cross-encoder score replaces the Hit.score field, so downstream code
    sees a single comparable relevance score. Returns the top k.
    """
    if not hits:
        return []

    model = _load_model()
    pairs = [(query, hit.content) for hit in hits]
    scores = model.predict(pairs)

    rescored = [replace(hit, score=float(s)) for hit, s in zip(hits, scores)]
    rescored.sort(key=lambda h: h.score, reverse=True)
    return rescored[:k]


def search_hybrid_rerank(
    query: str,
    k: int = 10,
    topic: str | None = None,
    candidate_k: int = CANDIDATE_K,
) -> list[Hit]:
    """First-stage hybrid RRF retrieval, then cross-encoder reranking.

    Shares the signature of the other retrievers so the evaluation harness
    can call it interchangeably.
    """
    candidates = search_hybrid(query, k=candidate_k, topic=topic)
    return rerank(query, candidates, k=k)


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else (
        "What interventions reduce 30-day readmissions for heart failure patients?"
    )
    print(f"query: {q}\n")
    hits = search_hybrid_rerank(q, k=10)
    for i, h in enumerate(hits, start=1):
        title = (h.title or h.content[:80]).strip()
        print(f"{i:2d}. [{h.score:+.3f}] {h.doc_id}  {title[:90]}")