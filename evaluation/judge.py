# evaluation/judge.py
"""LLM-as-judge evaluation across all five pipeline variants.

Scores each generated answer on three dimensions:
  - groundedness: are the answer's claims supported by the retrieved context?
    LLM-judged, 0-2. Only meaningful when there is context, so it is scored
    for the four retrieval modes and skipped for no_retrieval.
  - completeness: does the answer address what the question asks? LLM-judged,
    0-2, scored for all five variants.
  - citation_validity: fraction of cited PMIDs that exist in the corpus.
    Checked mechanically against the documents table, not by a judge. Scored
    for all five. This is the dimension on which the no_retrieval baseline
    fails: it writes fluent answers citing PMIDs that are not in the corpus.

Answer quality (completeness) and citation validity are scored separately and
never combined, because the no_retrieval baseline tends to score well on
completeness while fabricating its sources — a single blended score would rank
it above the grounded pipeline.

Runs 5 variants x N questions. Resumable: results already in the output file
are skipped, so an interrupted run resumes without re-spending on completed
(question, variant) pairs.

Usage:
    uv run python -m evaluation.judge
    uv run python -m evaluation.judge --modes hybrid_rerank no_retrieval
    uv run python -m evaluation.judge --out evaluation/judge_results.jsonl
    uv run python -m evaluation.judge --report        # aggregate existing results
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agents.generate import MODES, generate
from evaluation.retrieval_metrics import load_questions
from ingestion.load import connect

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from openai import RateLimitError

load_dotenv()

JUDGE_MODEL = "gpt-4o"
DEFAULT_OUT = Path(__file__).parent / "judge_results.jsonl"

_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Tolerates [PMID:123], [PMID: 123], and case variation.
PMID_RE = re.compile(r"\[pmid:\s*(\d+)\]", re.IGNORECASE)


# ---------------------------------------------------------------- citation check


def extract_pmids(answer: str) -> list[str]:
    return PMID_RE.findall(answer)


def corpus_pmids() -> set[str]:
    """All doc_ids present in the corpus, for mechanical citation validation."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM documents")
        return {row[0] if not isinstance(row, dict) else row["doc_id"] for row in cur.fetchall()}


def citation_validity(answer: str, valid: set[str]) -> dict:
    """Fraction of cited PMIDs that exist in the corpus.

    Returns counts as well as the fraction so the report can distinguish
    'cited nothing' from 'cited only invalid PMIDs'.
    """
    cited = extract_pmids(answer)
    if not cited:
        return {"n_cited": 0, "n_valid": 0, "validity": None, "invalid_pmids": []}
    valid_hits = [p for p in cited if p in valid]
    invalid = [p for p in cited if p not in valid]
    return {
        "n_cited": len(cited),
        "n_valid": len(valid_hits),
        "validity": len(valid_hits) / len(cited),
        "invalid_pmids": invalid,
    }


# ---------------------------------------------------------------- LLM judging


GROUNDEDNESS_SYSTEM = """You are evaluating whether an answer is grounded in the \
evidence it was given. You will see a question, a set of evidence documents, and \
an answer. Judge only whether the answer's factual claims are supported by the \
evidence documents — not whether the answer is complete, well written, or \
otherwise good.

Score 0, 1, or 2:
- 2: every substantive claim is supported by the evidence documents.
- 1: mostly supported, but at least one claim goes beyond or is not traceable \
to the evidence.
- 0: major claims are unsupported by the evidence, or contradict it.

Respond with a JSON object: {"score": <0|1|2>, "reason": "<one sentence>"}."""

COMPLETENESS_SYSTEM = """You are evaluating whether an answer addresses the \
question that was asked. Judge coverage of the question, not sourcing, and not \
writing style or formatting. An answer that is well-formatted but thin should \
score lower than a plain answer that covers the question thoroughly.

Score 0, 1, or 2:
- 2: fully addresses what the question asks.
- 1: partially addresses it; a relevant part is missing or underdeveloped.
- 0: does not address the question, or answers a different question.

Respond with a JSON object: {"score": <0|1|2>, "reason": "<one sentence>"}."""


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(6),
)
def _judge(system: str, user: str) -> dict:
    resp = _client.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        obj = json.loads(raw)
        score = int(obj.get("score"))
        return {"score": score, "reason": obj.get("reason", "")}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"score": None, "reason": f"unparseable judge output: {raw[:120]}"}


def judge_groundedness(question: str, contexts, answer: str) -> dict:
    evidence = "\n\n---\n\n".join(
        f"[PMID:{h.doc_id}] {h.title or ''}\n{h.content}" for h in contexts
    )
    user = (
        f"Question: {question}\n\nEvidence documents:\n\n{evidence}\n\n"
        f"Answer to evaluate:\n\n{answer}"
    )
    return _judge(GROUNDEDNESS_SYSTEM, user)


def judge_completeness(question: str, answer: str) -> dict:
    user = f"Question: {question}\n\nAnswer to evaluate:\n\n{answer}"
    return _judge(COMPLETENESS_SYSTEM, user)


# ---------------------------------------------------------------- run


def load_done(out: Path) -> set[tuple[str, str]]:
    """Return (question_id, mode) pairs already scored, for resumability."""
    done: set[tuple[str, str]] = set()
    if out.exists():
        with out.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                done.add((str(row["question_id"]), row["mode"]))
    return done


def run(modes: list[str], out: Path, k: int = 5) -> None:
    questions = load_questions()
    valid_pmids = corpus_pmids()
    done = load_done(out)

    total = len(questions) * len(modes)
    completed = 0

    with out.open("a") as fh:
        for q in questions:
            qid = str(q["id"])
            for mode in modes:
                completed += 1
                if (qid, mode) in done:
                    continue

                gen = generate(q["question"], mode=mode, k=k)

                cit = citation_validity(gen.answer, valid_pmids)

                grounded = None
                if mode != "no_retrieval" and gen.contexts:
                    grounded = judge_groundedness(q["question"], gen.contexts, gen.answer)

                complete = judge_completeness(q["question"], gen.answer)

                record = {
                    "question_id": qid,
                    "question": q["question"],
                    "mode": mode,
                    "answer": gen.answer,
                    "context_pmids": gen.context_pmids,
                    "groundedness": grounded,
                    "completeness": complete,
                    "citation": cit,
                    "prompt_tokens": gen.prompt_tokens,
                    "completion_tokens": gen.completion_tokens,
                    "gen_model": gen.model,
                    "judge_model": JUDGE_MODEL,
                }
                fh.write(json.dumps(record) + "\n")
                fh.flush()
                print(f"[{completed}/{total}] {qid} {mode}: "
                      f"grounded={grounded['score'] if grounded else '-'} "
                      f"complete={complete['score']} "
                      f"cite={cit['n_valid']}/{cit['n_cited']}")


# ---------------------------------------------------------------- report


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(v):
    return "n/a" if v is None else f"{v:.3f}"


def report(out: Path, markdown: bool = False) -> None:
    rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    by_mode: dict[str, list[dict]] = {}
    for r in rows:
        by_mode.setdefault(r["mode"], []).append(r)

    header = ["variant", "n", "groundedness", "completeness",
              "citation_validity", "answers_w_invalid_cite"]
    table = []
    # Preserve MODES order where present.
    for mode in [m for m in MODES if m in by_mode]:
        recs = by_mode[mode]
        g = _mean([r["groundedness"]["score"] for r in recs if r["groundedness"]])
        c = _mean([r["completeness"]["score"] for r in recs if r["completeness"]])
        cv = _mean([r["citation"]["validity"] for r in recs])
        n_bad = sum(1 for r in recs if r["citation"]["invalid_pmids"])
        table.append([mode, str(len(recs)), _fmt(g), _fmt(c), _fmt(cv), str(n_bad)])

    if markdown:
        print("| " + " | ".join(header) + " |")
        print("|" + "|".join(["---"] * len(header)) + "|")
        for row in table:
            print("| " + " | ".join(row) + " |")
    else:
        widths = [max(len(header[i]), *(len(r[i]) for r in table)) for i in range(len(header))]
        line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
        print(line)
        print("-" * len(line))
        for row in table:
            print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


# ---------------------------------------------------------------- entrypoint


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--modes", nargs="+", default=MODES,
                   help=f"subset of {MODES}")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--report", action="store_true",
                   help="aggregate the existing output file, do not run")
    p.add_argument("--markdown", action="store_true")
    args = p.parse_args()

    if args.report:
        report(args.out, markdown=args.markdown)
        return

    for m in args.modes:
        if m not in MODES:
            raise SystemExit(f"unknown mode {m!r}; expected subset of {MODES}")

    run(args.modes, args.out, k=args.k)
    print()
    report(args.out, markdown=args.markdown)


if __name__ == "__main__":
    main()