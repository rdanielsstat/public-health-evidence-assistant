# retrieval/search.py
"""Lexical and dense retrieval over the chunk corpus."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI
from pgvector.psycopg import register_vector

from ingestion.load import connect

import re

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was",
    "were", "what", "which", "how", "does", "did", "can", "who", "why",
    "has", "have", "had", "its", "not", "but", "all", "any", "may",
}

load_dotenv()

EMBED_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536

_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    title: str | None
    content: str
    score: float
    topics: list[str]
    published_year: int | None
    url: str | None
    source: str | None = None


def embed_query(text: str) -> list[float]:
    resp = _client.embeddings.create(
        input=[text], model=EMBED_MODEL, dimensions=DIMENSIONS
    )
    return resp.data[0].embedding


SELECT_FIELDS = """
    c.id           AS chunk_id,
    d.doc_id       AS doc_id,
    d.title        AS title,
    c.content      AS content,
    d.topics       AS topics,
    d.published_year AS published_year,
    d.url          AS url,
    d.source       AS source
"""

def to_or_tsquery(text: str) -> str:
    """Build an OR-joined tsquery so partial matches rank rather than filter."""
    words = [
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 2 and w not in STOPWORDS
    ]
    return " | ".join(words) if words else "''"

def search_lexical(query: str, k: int = 10, topic: str | None = None) -> list[Hit]:
    sql = f"""
        SELECT {SELECT_FIELDS},
               ts_rank_cd(c.content_tsv, to_tsquery('english', %(q)s)) AS score
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.content_tsv @@ to_tsquery('english', %(q)s)
          AND (%(topic)s::text IS NULL OR %(topic)s::text = ANY(d.topics))
        ORDER BY score DESC
        LIMIT %(k)s
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, {"q": to_or_tsquery(query), "k": k, "topic": topic})
        return [Hit(**row) for row in cur.fetchall()]

def search_dense(query: str, k: int = 10, topic: str | None = None) -> list[Hit]:
    vec = embed_query(query)
    sql = f"""
        SELECT {SELECT_FIELDS},
               1 - (e.embedding <=> %(vec)s::vector) AS score
        FROM chunk_embeddings e
        JOIN chunks c    ON c.id = e.chunk_id
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE e.model = %(model)s
          AND (%(topic)s::text IS NULL OR %(topic)s::text = ANY(d.topics))
        ORDER BY e.embedding <=> %(vec)s::vector
        LIMIT %(k)s
    """
    with connect() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(sql, {
                "vec": vec, "k": k, "topic": topic, "model": EMBED_MODEL
            })
            return [Hit(**row) for row in cur.fetchall()]


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "interventions to reduce heart failure readmissions"

    for name, fn in [("LEXICAL", search_lexical), ("DENSE", search_dense)]:
        print(f"\n{'=' * 78}\n{name}: {q}\n{'=' * 78}")
        for i, h in enumerate(fn(q, k=5), 1):
            print(f"{i}. [{h.score:.4f}] {h.published_year} {(h.title or '')[:70]}")