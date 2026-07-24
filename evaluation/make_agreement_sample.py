# evaluation/make_agreement_sample.py
"""Write a blind stratified sample for manual relevance grading.

Pulls chunk content from Postgres so the manual grader sees the same
information the automated grader saw (title + abstract), rather than titles
alone.

    uv run python -m evaluation.make_agreement_sample
    uv run python -m evaluation.make_agreement_sample --per-grade 15 --seed 7
"""

import argparse
import json
import random
import textwrap
from pathlib import Path

from ingestion.load import connect

QRELS = Path("evaluation/qrels.jsonl")
OUT = Path("evaluation/agreement_sample.txt")
KEY = Path("evaluation/agreement_key.json")

RUBRIC = """\
GRADING RUBRIC

  2 = Highly relevant. The document directly addresses the question and a
      good answer would cite it.
  1 = Partially relevant. The document is on-topic and contributes context,
      but does not directly answer the question.
  0 = Not relevant. The document shares vocabulary or subject area but would
      not help answer the question.

Be strict. Reserve 2 for documents that genuinely answer the question asked.

Record one grade per item, in order. The automated grades are withheld from
this file and stored in agreement_key.json.
"""


def fetch_content(chunk_ids: list[int]) -> dict[int, str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, content FROM chunks WHERE id = ANY(%s)",
            (chunk_ids,),
        )
        return {r["id"]: r["content"] for r in cur.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-grade", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="interleave grades so the strata are not visible in file order",
    )
    args = parser.parse_args()

    rows = [json.loads(line) for line in QRELS.open()]
    rng = random.Random(args.seed)

    sample = []
    for grade in (0, 1, 2):
        pool = [r for r in rows if r["grade"] == grade]
        if len(pool) < args.per_grade:
            raise SystemExit(
                f"only {len(pool)} rows with grade {grade}, "
                f"need {args.per_grade}"
            )
        sample.extend(rng.sample(pool, args.per_grade))

    if args.shuffle:
        rng.shuffle(sample)

    content = fetch_content([r["chunk_id"] for r in sample])

    lines = [RUBRIC, "=" * 78, ""]
    for i, r in enumerate(sample, 1):
        text = content.get(r["chunk_id"], "(content not found)")
        lines.append(f"--- {i} of {len(sample)}  [{r['question_id']}] ---")
        lines.append("")
        lines.append("QUESTION")
        lines.append(textwrap.fill(r["question"], width=76, initial_indent="  ",
                                   subsequent_indent="  "))
        lines.append("")
        lines.append("DOCUMENT")
        for para in text.split("\n\n"):
            lines.append(textwrap.fill(para, width=76, initial_indent="  ",
                                       subsequent_indent="  "))
            lines.append("")
        lines.append("GRADE: ___")
        lines.append("")
        lines.append("")

    OUT.write_text("\n".join(lines))

    KEY.write_text(json.dumps(
        [
            {
                "position": i,
                "question_id": r["question_id"],
                "chunk_id": r["chunk_id"],
                "model_grade": r["grade"],
            }
            for i, r in enumerate(sample, 1)
        ],
        indent=2,
    ))

    print(f"wrote {len(sample)} items to {OUT}")
    print(f"withheld grades in {KEY}")
    if not args.shuffle:
        print(
            f"note: items 1-{args.per_grade} are model grade 0, "
            f"{args.per_grade + 1}-{2 * args.per_grade} are grade 1, "
            f"{2 * args.per_grade + 1}-{3 * args.per_grade} are grade 2. "
            "Use --shuffle to hide this."
        )


if __name__ == "__main__":
    main()