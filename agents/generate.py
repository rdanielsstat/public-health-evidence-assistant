# agents/generate.py
"""Answer generation over retrieved evidence, with a retrieval-mode parameter.

One generate() entry point covers all five pipeline variants. Four modes
retrieve evidence and ground the answer in it; no_retrieval answers from the
model's parametric knowledge and exists as the baseline that motivates scoring
citation validity separately from answer quality.

The model is instructed to cite sources inline as [PMID:12345678]. Citations
are not validated here — that is the judge's mechanical check against the
documents table. Generation only produces the answer and returns the retrieved
context alongside it so the judge and the interface can inspect both.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import OpenAI
from tenacity import stop_after_attempt

from retrieval.search import Hit, search_dense, search_lexical
from retrieval.fusion import search_hybrid
from retrieval.rerank import search_hybrid_rerank

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from openai import RateLimitError

load_dotenv()

GEN_MODEL = "gpt-4o"
DEFAULT_TOP_K = 5

_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Retrieval mode -> retriever callable. no_retrieval is handled separately
# (it has no retriever). All retrievers share the (query, k, topic) signature.
RETRIEVERS = {
    "lexical_only": search_lexical,
    "dense_only": search_dense,
    "hybrid_rrf": search_hybrid,
    "hybrid_rerank": search_hybrid_rerank,
}

MODES = ["no_retrieval", *RETRIEVERS.keys()]


SYSTEM_GROUNDED = """You are a public health evidence assistant. Answer the \
question using only the provided evidence documents. Each document is labelled \
with its PMID.

Rules:
- Base every factual claim on the provided documents. Do not use outside knowledge.
- Cite the source of each claim inline as [PMID:<pmid>], using the PMID given \
in the document label. Cite only PMIDs that appear in the provided documents.
- If the documents do not contain enough information to answer, say so plainly \
rather than filling the gap from general knowledge.
- Be concise and specific. Do not pad the answer."""

SYSTEM_UNGROUNDED = """You are a public health evidence assistant. Answer the \
question about inpatient healthcare quality and safety.

Cite supporting evidence inline as [PMID:<pmid>] where you can."""


@dataclass
class GenerationResult:
    question: str
    mode: str
    answer: str
    contexts: list[Hit] = field(default_factory=list)
    context_pmids: list[str] = field(default_factory=list)
    model: str = GEN_MODEL
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _format_context(hits: list[Hit]) -> str:
    """Render retrieved documents into a labelled block for the prompt."""
    blocks = []
    for h in hits:
        title = h.title or ""
        blocks.append(f"[PMID:{h.doc_id}] {title}\n{h.content}")
    return "\n\n---\n\n".join(blocks)


@retry(
    retry=retry_if_exception_type(RateLimitError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(6),
)
def _complete(system: str, user: str):
    return _client.chat.completions.create(
        model=GEN_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )


def generate(
    question: str,
    mode: str = "hybrid_rerank",
    k: int = DEFAULT_TOP_K,
    topic: str | None = None,
    contexts: list[Hit] | None = None,
) -> GenerationResult:
    """Answer a question under one retrieval mode.

    For retrieval modes, the top-k documents are retrieved and passed to the
    model as grounding context. For no_retrieval, the model answers from
    parametric knowledge with no context.

    If `contexts` is supplied, retrieval is skipped and the given documents are
    used as-is. This lets a caller that controls retrieval itself (e.g. the
    query router, which retrieves per sub-question and merges) reuse this exact
    grounded generation path. `contexts` is ignored for no_retrieval.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    if mode == "no_retrieval":
        contexts = []
        system = SYSTEM_UNGROUNDED
        user = question
    else:
        if contexts is None:
            contexts = RETRIEVERS[mode](question, k=k, topic=topic)
        system = SYSTEM_GROUNDED
        if contexts:
            user = f"Question: {question}\n\nEvidence documents:\n\n{_format_context(contexts)}"
        else:
            user = (
                f"Question: {question}\n\n"
                "No evidence documents were retrieved. State that the corpus "
                "does not contain information to answer this question."
            )

    resp = _complete(system, user)

    answer = resp.choices[0].message.content or ""
    usage = resp.usage

    return GenerationResult(
        question=question,
        mode=mode,
        answer=answer,
        contexts=contexts,
        context_pmids=[h.doc_id for h in contexts],
        model=GEN_MODEL,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
    )


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or (
        "What interventions reduce 30-day readmissions for heart failure patients?"
    )
    mode = os.environ.get("MODE", "hybrid_rerank")
    result = generate(q, mode=mode)

    print(f"mode:     {result.mode}")
    print(f"question: {result.question}")
    print(f"context:  {result.context_pmids}")
    print(f"tokens:   {result.prompt_tokens} in / {result.completion_tokens} out")
    print(f"\n{result.answer}")