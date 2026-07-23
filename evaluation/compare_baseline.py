# evaluation/compare_baseline.py
"""Answer a question with and without retrieval, print both for inspection.

    uv run python -m evaluation.compare_baseline "your question here"
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from retrieval.search import search_dense, Hit

load_dotenv()

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
MODEL = "gpt-4o-mini"

GROUNDED_PROMPT = """You answer questions about inpatient healthcare quality \
using only the provided sources.

Rules:
- Use only information from the sources below.
- Cite the PMID for every claim, like [PMID 12345678].
- If the sources do not answer the question, say so.

SOURCES:
{context}

QUESTION: {question}"""

BASELINE_PROMPT = """You answer questions about inpatient healthcare quality.

Answer from your own knowledge. Cite specific studies by PMID where you \
can, like [PMID 12345678].

QUESTION: {question}"""


def format_context(hits: list[Hit]) -> str:
    return "\n\n".join(
        f"[PMID {h.doc_id}] ({h.published_year}) {h.title}\n{h.content}"
        for h in hits
    )


def ask(prompt: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return resp.choices[0].message.content


def main() -> None:
    question = " ".join(sys.argv[1:])
    if not question:
        raise SystemExit("usage: python -m evaluation.compare_baseline <question>")

    hits = search_dense(question, k=5)

    print("=" * 78)
    print("RETRIEVED")
    print("=" * 78)
    for h in hits:
        print(f"  [{h.score:.3f}] PMID {h.doc_id} ({h.published_year}) {(h.title or '')[:60]}")

    print()
    print("=" * 78)
    print("WITH RETRIEVAL")
    print("=" * 78)
    print(ask(GROUNDED_PROMPT.format(context=format_context(hits), question=question)))

    print()
    print("=" * 78)
    print("NO RETRIEVAL (baseline)")
    print("=" * 78)
    print(ask(BASELINE_PROMPT.format(question=question)))


if __name__ == "__main__":
    main()