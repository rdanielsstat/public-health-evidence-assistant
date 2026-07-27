# ingestion/chunking.py
"""Split long document text into embedding-sized chunks.

PubMed abstracts are short and are indexed whole (one chunk per document). CMS
policy descriptions are longer and benefit from splitting so that a retrieved
chunk is a focused, citable passage rather than a whole multi-topic program
description. This module provides that split.

The splitter is deliberately simple and deterministic: it packs whole sentences
into chunks up to a word budget, with a small overlap so a concept spanning a
sentence boundary is reachable from either side. Text shorter than the budget
returns as a single chunk, so short inputs (including any PubMed text routed
through here) are unaffected.
"""

from __future__ import annotations

import re

# Word budget per chunk. text-embedding-3-small handles far more than this; the
# budget is chosen so a chunk is a focused passage, not to avoid truncation.
MAX_WORDS = 220
# Sentences of overlap carried from the end of one chunk into the next.
OVERLAP_SENTENCES = 1

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _sentences(text: str) -> list[str]:
    """Split text into sentences on terminal punctuation followed by a capital.
    Falls back to the whole text as one sentence if no boundary is found."""
    text = text.strip()
    if not text:
        return []
    parts = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    return parts or [text]


def split_text(text: str, max_words: int = MAX_WORDS,
               overlap_sentences: int = OVERLAP_SENTENCES) -> list[str]:
    """Pack sentences into chunks of at most max_words words, with sentence
    overlap between consecutive chunks. Returns [text] when it fits in one
    chunk. Never splits mid-sentence."""
    text = (text or "").strip()
    if not text:
        return []

    if len(text.split()) <= max_words:
        return [text]

    sentences = _sentences(text)
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    for sent in sentences:
        w = len(sent.split())
        # A single sentence longer than the budget becomes its own chunk rather
        # than being broken apart.
        if w > max_words:
            if current:
                chunks.append(" ".join(current))
                current, current_words = [], 0
            chunks.append(sent)
            continue

        if current_words + w > max_words and current:
            chunks.append(" ".join(current))
            # Start the next chunk with the overlap tail of the previous one.
            tail = current[-overlap_sentences:] if overlap_sentences else []
            current = list(tail)
            current_words = sum(len(s.split()) for s in current)

        current.append(sent)
        current_words += w

    if current:
        chunks.append(" ".join(current))

    return chunks
