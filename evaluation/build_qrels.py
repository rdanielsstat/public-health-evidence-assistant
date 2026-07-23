# evaluation/build_qrels.py
"""Pool retrieval candidates and label them for relevance.

For each question, retrieve top-k from both lexical and dense, pool the union,
and grade every (question, chunk) pair 0/1/2. Writes evaluation/qrels.jsonl.

    uv run python -m evaluation.build_qrels
    uv run python -m evaluation.build_qrels --pool-k 10 --model gpt-4o-mini
"""

import argparse
import json
import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from retrieval.search import search_lexical, search_dense

load_dotenv()

log = logging.getLogger(__name__)

QUESTIONS = Path("evaluation/questions.yaml")
OUT = Path("evaluation/qrels.jsonl")

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

GRADE_PROMPT = """You are grading search results for a system that answers \
questions about inpatient healthcare quality and safety using peer-reviewed \
abstracts.

Grade how well this document would help answer the question.

2 = Highly relevant. The document directly addresses the question and a good
    answer would cite it.
1 = Partially relevant. The document is on-topic and contributes context, but
    does not directly answer the question.
0 = Not relevant. The document shares vocabulary or subject area but would not
    help answer the question.

Be strict. Most pooled results are 0 or 1. Reserve 2 for documents that
genuinely answer the question asked.

QUESTION: {question}

DOCUMENT:
{content}

Respond with exactly two lines:
GRADE: <0, 1, or 2>
REASON: <one sentence>"""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30))
def grade(question: str, content: str, model: str) -> tuple[int, str]:
    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": GRADE_PROMPT.format(question=question, content=content[:4000]),
        }],
        temperature=0,
    )
    text = resp.choices[0].message.content or ""

    m = re.search(r"GRADE:\s*([012])", text)
    if not m:
        log.warning("unparseable grade response: %r", text[:200])
        return 0, "unparseable response"

    grade_val = int(m.group(1))
    reason_m = re.search(r"REASON:\s*(.+)", text)
    reason = reason_m.group(1).strip() if reason_m else ""
    return grade_val, reason


def pool(question: str, k: int) -> dict[int, dict]:
    """Union of lexical and dense top-k, keyed by chunk_id."""
    pooled: dict[int, dict] = {}

    for source, hits in [
        ("lexical", search_lexical(question, k=k)),
        ("dense", search_dense(question, k=k)),
    ]:
        for rank, h in enumerate(hits, 1):
            entry = pooled.setdefault(h.chunk_id, {
                "chunk_id": h.chunk_id,
                "doc_id": h.doc_id,
                "title": h.title,
                "content": h.content,
                "topics": h.topics,
                "published_year": h.published_year,
                "found_by": [],
                "ranks": {},
            })
            entry["found_by"].append(source)
            entry["ranks"][source] = rank

    return pooled


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-k", type=int, default=10)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--limit", type=int, help="only process first N questions")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    questions = yaml.safe_load(QUESTIONS.read_text())
    if args.limit:
        questions = questions[:args.limit]

    rows = []
    for q in questions:
        pooled = pool(q["question"], args.pool_k)
        log.info(
            "%s: pooled %d unique chunks (%d in both)",
            q["id"], len(pooled),
            sum(1 for e in pooled.values() if len(e["found_by"]) == 2),
        )

        for entry in pooled.values():
            g, reason = grade(q["question"], entry["content"], args.model)
            rows.append({
                "question_id": q["id"],
                "question": q["question"],
                "chunk_id": entry["chunk_id"],
                "doc_id": entry["doc_id"],
                "title": entry["title"],
                "topics": entry["topics"],
                "published_year": entry["published_year"],
                "found_by": sorted(entry["found_by"]),
                "ranks": entry["ranks"],
                "grade": g,
                "reason": reason,
                "grader_model": args.model,
            })

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    log.info("wrote %d judgments to %s", len(rows), OUT)

    # summary
    import collections
    by_grade = collections.Counter(r["grade"] for r in rows)
    log.info("grade distribution: %s", dict(sorted(by_grade.items())))

    per_q = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        per_q[r["question_id"]][r["grade"]] += 1

    log.info("")
    log.info("%-6s %5s %5s %5s %5s", "qid", "pool", "g0", "g1", "g2")
    for qid in sorted(per_q):
        c = per_q[qid]
        log.info("%-6s %5d %5d %5d %5d", qid, sum(c.values()), c[0], c[1], c[2])

    no_relevant = [qid for qid, c in per_q.items() if c[1] + c[2] == 0]
    if no_relevant:
        log.warning("questions with no relevant results: %s", no_relevant)


if __name__ == "__main__":
    main()