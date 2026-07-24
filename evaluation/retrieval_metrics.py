"""Retrieval metrics harness: Recall@5, Recall@10, MRR, NDCG@10.

Scores lexical / dense / hybrid against evaluation/qrels.jsonl.

Usage:
    uv run python -m evaluation.retrieval_metrics
    uv run python -m evaluation.retrieval_metrics --k 10 --rel-threshold 1
    uv run python -m evaluation.retrieval_metrics --markdown
    uv run python -m evaluation.retrieval_metrics --per-question
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

import yaml

from retrieval.fusion import search_hybrid
from retrieval.search import Hit, search_dense, search_lexical

QUESTIONS_PATH = Path(__file__).parent / "questions.yaml"
QRELS_PATH = Path(__file__).parent / "qrels.jsonl"

Retriever = Callable[..., list[Hit]]

RETRIEVERS: dict[str, Retriever] = {
    "lexical": search_lexical,
    "dense": search_dense,
    "hybrid_rrf": search_hybrid,
}


# ---------------------------------------------------------------- loading


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if isinstance(data, dict):
        data = data.get("questions", [])
    return list(data)


def load_qrels(path: Path = QRELS_PATH) -> dict[str, dict[int, int]]:
    """Return {question_id: {chunk_id: grade}}."""
    qrels: dict[str, dict[int, int]] = defaultdict(dict)
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            qid = str(row["question_id"])
            chunk_id = int(row["chunk_id"])
            qrels[qid][chunk_id] = int(row["grade"])
    return dict(qrels)


# ---------------------------------------------------------------- metrics


def recall_at_k(ranked: list[int], relevant: set[int], k: int) -> float | None:
    if not relevant:
        return None
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[int], relevant: set[int]) -> float | None:
    if not relevant:
        return None
    for rank, chunk_id in enumerate(ranked, start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def dcg(gains: Iterable[float]) -> float:
    return sum(g / math.log2(rank + 1) for rank, g in enumerate(gains, start=1))


def ndcg_at_k(ranked: list[int], grades: dict[int, int], k: int) -> float | None:
    """Graded NDCG with gain = 2**grade - 1. Unjudged documents are gain 0."""
    if not any(g > 0 for g in grades.values()):
        return None
    actual = dcg(2 ** grades.get(cid, 0) - 1 for cid in ranked[:k])
    ideal_grades = sorted(grades.values(), reverse=True)[:k]
    ideal = dcg(2**g - 1 for g in ideal_grades)
    return actual / ideal if ideal > 0 else None


# ---------------------------------------------------------------- evaluation


def mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def evaluate_retriever(
    name: str,
    retriever: Retriever,
    questions: list[dict],
    qrels: dict[str, dict[int, int]],
    k: int = 10,
    rel_threshold: int = 1,
    use_topic_filter: bool = False,
) -> dict:
    """Run one retriever over all judged questions and aggregate metrics.

    Only questions that have at least one judgment at or above rel_threshold
    are scored, so out-of-scope questions (q23, q24) are excluded rather than
    counted as zeros.
    """
    per_question: list[dict] = []

    for q in questions:
        qid = str(q["id"])
        grades = qrels.get(qid)
        if not grades:
            continue
        relevant = {cid for cid, g in grades.items() if g >= rel_threshold}
        if not relevant:
            continue

        topic = None
        if use_topic_filter:
            topics = q.get("topics") or []
            topic = topics[0] if len(topics) == 1 else None

        hits = retriever(q["question"], k=k, topic=topic)
        ranked = [h.chunk_id for h in hits]

        per_question.append(
            {
                "question_id": qid,
                "recall@5": recall_at_k(ranked, relevant, 5),
                f"recall@{k}": recall_at_k(ranked, relevant, k),
                "mrr": reciprocal_rank(ranked, relevant),
                f"ndcg@{k}": ndcg_at_k(ranked, grades, k),
                "n_relevant": len(relevant),
                "n_retrieved_judged": sum(1 for cid in ranked if cid in grades),
            }
        )

    metric_names = ["recall@5", f"recall@{k}", "mrr", f"ndcg@{k}"]
    aggregate = {m: mean([row[m] for row in per_question]) for m in metric_names}
    aggregate["judged_coverage@k"] = mean(
        [row["n_retrieved_judged"] / k for row in per_question]
    )

    return {
        "name": name,
        "n_questions": len(per_question),
        "aggregate": aggregate,
        "per_question": per_question,
    }


# ---------------------------------------------------------------- reporting


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def print_table(results: list[dict], k: int, markdown: bool = False) -> None:
    columns = ["recall@5", f"recall@{k}", "mrr", f"ndcg@{k}", "judged_coverage@k"]
    header = ["retriever", *columns]

    rows = [[r["name"], *[fmt(r["aggregate"][c]) for c in columns]] for r in results]

    if markdown:
        print("| " + " | ".join(header) + " |")
        print("|" + "|".join(["---"] * len(header)) + "|")
        for row in rows:
            print("| " + " | ".join(row) + " |")
        return

    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))


def print_per_question(results: list[dict], k: int) -> None:
    metric = f"ndcg@{k}"
    qids = [row["question_id"] for row in results[0]["per_question"]]
    header = ["question", *[r["name"] for r in results]]
    print("\nPer-question " + metric)
    print("  ".join(header))
    for i, qid in enumerate(qids):
        cells = [qid] + [fmt(r["per_question"][i][metric]) for r in results]
        print("  ".join(cells))


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--rel-threshold",
        type=int,
        default=1,
        help="minimum grade counted as relevant for recall and MRR (NDCG always graded)",
    )
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--per-question", action="store_true")
    parser.add_argument(
        "--topic-filter",
        action="store_true",
        help="pass the question's topic to the retriever when it has exactly one",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    questions = load_questions()
    qrels = load_qrels()

    results = [
        evaluate_retriever(
            name,
            retriever,
            questions,
            qrels,
            k=args.k,
            rel_threshold=args.rel_threshold,
            use_topic_filter=args.topic_filter,
        )
        for name, retriever in RETRIEVERS.items()
    ]

    print(
        f"{results[0]['n_questions']} questions scored, "
        f"relevance threshold grade >= {args.rel_threshold}\n"
    )
    print_table(results, args.k, markdown=args.markdown)

    if args.per_question:
        print_per_question(results, args.k)

    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()