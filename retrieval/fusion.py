"""Reciprocal rank fusion over lexical and dense retrieval."""

from __future__ import annotations

from dataclasses import replace

from retrieval.search import Hit, search_dense, search_lexical

RRF_K = 60
CANDIDATE_MULTIPLIER = 5
MIN_CANDIDATES = 50


def rrf_fuse(
    ranked_lists: list[list[Hit]],
    k: int = 10,
    rrf_k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[Hit]:
    """Fuse ranked lists by reciprocal rank fusion.

    Each list contributes 1 / (rrf_k + rank) per document, rank starting at 1.
    Documents absent from a list simply contribute nothing from it.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights must have the same length as ranked_lists")

    scores: dict[int, float] = {}
    exemplar: dict[int, Hit] = {}

    for weight, hits in zip(weights, ranked_lists):
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (rrf_k + rank)
            if hit.chunk_id not in exemplar:
                exemplar[hit.chunk_id] = hit

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [replace(exemplar[chunk_id], score=score) for chunk_id, score in ordered[:k]]


def search_hybrid(
    query: str,
    k: int = 10,
    topic: str | None = None,
    rrf_k: int = RRF_K,
    candidate_k: int | None = None,
) -> list[Hit]:
    """Hybrid retrieval: RRF over Postgres FTS and pgvector cosine.

    Rank-based fusion is used rather than score interpolation because
    ts_rank_cd and cosine distance are not comparable in magnitude and
    ts_rank_cd is not normalised across queries.
    """
    if candidate_k is None:
        candidate_k = max(MIN_CANDIDATES, k * CANDIDATE_MULTIPLIER)

    lexical = search_lexical(query, k=candidate_k, topic=topic)
    dense = search_dense(query, k=candidate_k, topic=topic)
    return rrf_fuse([lexical, dense], k=k, rrf_k=rrf_k)