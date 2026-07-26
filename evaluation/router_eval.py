# evaluation/router_eval.py
"""Evaluate the query router: routing accuracy plus a retrieval-collapse check.

Two things are measured over the 24-question set:

1. Routing accuracy. Ground truth comes from the question file: a question is
   multi-part iff it has a topics_intent that covers more topics than its
   measured `topics` (i.e. len(topics_intent) > len(topics)). This is exactly
   the annotation that recorded the collapse in the first place. The router's
   route decision is scored against that label: precision, recall, and the
   specific misroutes.

2. Retrieval-collapse fix. For the questions labelled multi-part, the router
   retrieves per sub-question and merges. We check whether the merged context
   now covers the intended topics that the single-query path missed, using the
   topics recorded on each retrieved document.

To keep cost honest, only the multi-part questions (and any the router flags
multi-part) get a full gpt-4o answer; clearly single-part questions are routed
and their routing decision recorded, but not answered, since answering them
adds cost without testing anything the batch judge did not already cover.

Usage:
    uv run python -m evaluation.router_eval
    uv run python -m evaluation.router_eval --answer-all   # answer every question
    uv run python -m evaluation.router_eval --out evaluation/router_results.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agents.router import _route_call, answer
from evaluation.retrieval_metrics import load_questions
from ingestion.load import connect

DEFAULT_OUT = Path(__file__).parent / "router_results.jsonl"


# ---------------------------------------------------------------- ground truth


def is_multipart(q: dict) -> bool:
    """Ground-truth multi-part label from the question annotations.

    Multi-part iff topics_intent exists and covers more topics than the
    measured `topics`. This mirrors how the collapse was annotated: `topics` is
    what single-query retrieval actually surfaced, topics_intent is what the
    question was designed to cover.
    """
    intent = q.get("topics_intent")
    topics = q.get("topics") or []
    if not intent:
        return False
    return len(set(intent)) > len(set(topics))


# ---------------------------------------------------------------- topic lookup


def doc_topics(pmids: list[str]) -> dict[str, list[str]]:
    """Map each doc_id to its topics array, for the collapse check."""
    if not pmids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, topics FROM documents WHERE doc_id = ANY(%(ids)s)",
            {"ids": pmids},
        )
        out = {}
        for row in cur.fetchall():
            if isinstance(row, dict):
                out[row["doc_id"]] = row["topics"]
            else:
                out[row[0]] = row[1]
        return out


def covered_topics(pmids: list[str]) -> set[str]:
    """Union of topics across a set of retrieved documents."""
    m = doc_topics(pmids)
    covered: set[str] = set()
    for ts in m.values():
        covered.update(ts or [])
    return covered


# ---------------------------------------------------------------- evaluation


def evaluate(answer_all: bool, out: Path, k: int = 5) -> None:
    questions = load_questions()

    records = []
    # Confusion counts for routing accuracy.
    tp = fp = tn = fn = 0

    for q in questions:
        qid = str(q["id"])
        gold_multi = is_multipart(q)

        decision = _route_call(q["question"])
        pred_multi = decision["multipart"]

        if gold_multi and pred_multi:
            tp += 1
        elif not gold_multi and pred_multi:
            fp += 1
        elif not gold_multi and not pred_multi:
            tn += 1
        else:
            fn += 1

        rec = {
            "question_id": qid,
            "question": q["question"],
            "gold_multipart": gold_multi,
            "pred_multipart": pred_multi,
            "subquestions": decision["subquestions"],
            "topics_intent": q.get("topics_intent"),
            "topics_measured": q.get("topics"),
        }

        # Answer (and run the collapse check) when the question is multi-part by
        # either gold or prediction, or when --answer-all is set.
        if answer_all or gold_multi or pred_multi:
            routed = answer(q["question"], k=k)
            pmids = routed.generation.context_pmids
            covered = covered_topics(pmids)
            intent = set(q.get("topics_intent") or q.get("topics") or [])
            rec.update(
                {
                    "answered": True,
                    "context_pmids": pmids,
                    "context_topics": sorted(covered),
                    "intent_topics": sorted(intent),
                    "intent_topics_covered": sorted(intent & covered),
                    "intent_fully_covered": intent.issubset(covered) if intent else None,
                    "answer": routed.generation.answer,
                }
            )
        else:
            rec["answered"] = False

        records.append(rec)
        print(
            f"{qid}: gold={'multi' if gold_multi else 'single'} "
            f"pred={'multi' if pred_multi else 'single'} "
            f"{'OK' if gold_multi == pred_multi else 'MISROUTE'}"
            + (f"  covered={rec.get('intent_topics_covered')}" if rec.get("answered") else "")
        )

    out.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    # ---- routing accuracy report
    n = len(questions)
    acc = (tp + tn) / n if n else 0.0
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    print("\nRouting accuracy")
    print("----------------")
    print(f"questions:        {n}")
    print(f"gold multi-part:  {tp + fn}")
    print(f"pred multi-part:  {tp + fp}")
    print(f"correct:          {tp + tn}/{n}  (accuracy {acc:.3f})")
    print(f"precision:        {precision if precision is None else round(precision, 3)}")
    print(f"recall:           {recall if recall is None else round(recall, 3)}")
    print(f"confusion:        tp={tp} fp={fp} tn={tn} fn={fn}")

    misroutes = [r for r in records if r["gold_multipart"] != r["pred_multipart"]]
    if misroutes:
        print("\nMisroutes:")
        for r in misroutes:
            direction = (
                "missed multi-part" if r["gold_multipart"] else "over-split single-part"
            )
            print(f"  {r['question_id']}: {direction} — {r['question']}")

    # ---- collapse-fix report
    answered_multi = [
        r for r in records
        if r.get("answered") and r["gold_multipart"]
    ]
    if answered_multi:
        fixed = [r for r in answered_multi if r.get("intent_fully_covered")]
        print("\nRetrieval-collapse check (gold multi-part questions answered)")
        print("------------------------------------------------------------")
        print(f"multi-part answered:       {len(answered_multi)}")
        print(f"intended topics covered:   {len(fixed)}/{len(answered_multi)}")
        for r in answered_multi:
            print(
                f"  {r['question_id']}: intent={r['intent_topics']} "
                f"covered={r['intent_topics_covered']} "
                f"{'FULL' if r['intent_fully_covered'] else 'PARTIAL'}"
            )

    print(f"\nwrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--answer-all", action="store_true",
                   help="answer every question, not just multi-part ones")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--k", type=int, default=5)
    args = p.parse_args()
    evaluate(args.answer_all, args.out, k=args.k)


if __name__ == "__main__":
    main()