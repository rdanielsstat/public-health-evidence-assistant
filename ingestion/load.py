# ingestion/load.py
"""Load parsed PubMed records from JSONL into Postgres.

    uv run python -m ingestion.load
"""

import json
import logging
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

INPUT = Path("data/pubmed.jsonl")


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "phea"),
        password=os.getenv("POSTGRES_PASSWORD", "phea"),
        dbname=os.getenv("POSTGRES_DB", "phea"),
        row_factory=dict_row,
    )


def build_content(rec: dict) -> str:
    """What actually gets embedded: title prepended to abstract."""
    title = (rec.get("title") or "").strip()
    abstract = rec["abstract"].strip()
    return f"{title}\n\n{abstract}" if title else abstract


UPSERT_DOC = """
INSERT INTO documents (
    doc_id, source, topics, title, abstract, journal, doi, url,
    published_year, mesh_terms, publication_types, indexing_method
)
VALUES (
    %(doc_id)s, %(source)s, %(topics)s, %(title)s, %(abstract)s,
    %(journal)s, %(doi)s, %(url)s, %(published_year)s,
    %(mesh_terms)s, %(publication_types)s, %(indexing_method)s
)
ON CONFLICT (doc_id) DO UPDATE SET
    topics = (
        SELECT ARRAY(
            SELECT DISTINCT unnest(documents.topics || EXCLUDED.topics)
        )
    ),
    title = EXCLUDED.title,
    abstract = EXCLUDED.abstract,
    journal = EXCLUDED.journal,
    doi = EXCLUDED.doi,
    url = EXCLUDED.url,
    published_year = EXCLUDED.published_year,
    mesh_terms = EXCLUDED.mesh_terms,
    publication_types = EXCLUDED.publication_types,
    indexing_method = EXCLUDED.indexing_method
"""

UPSERT_CHUNK = """
INSERT INTO chunks (doc_id, chunk_index, content)
VALUES (%(doc_id)s, %(chunk_index)s, %(content)s)
ON CONFLICT (doc_id, chunk_index) DO UPDATE SET
    content = EXCLUDED.content
"""


def load(records: list[dict]) -> None:
    with connect() as conn, conn.cursor() as cur:
        for rec in records:
            cur.execute(UPSERT_DOC, rec)
            cur.execute(UPSERT_CHUNK, {
                "doc_id": rec["doc_id"],
                "chunk_index": 0,
                "content": build_content(rec),
            })
        conn.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not INPUT.exists():
        raise SystemExit(f"{INPUT} not found — run ingestion.fetch_pubmed first")

    records = [json.loads(line) for line in INPUT.open()]
    log.info("loading %d records", len(records))

    load(records)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM documents")
        n_docs = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM chunks")
        n_chunks = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM chunk_embeddings")
        n_embedded = cur.fetchone()["n"]

    log.info("documents: %d", n_docs)
    log.info("chunks: %d (%d embedded)", n_chunks, n_embedded)


if __name__ == "__main__":
    main()