# ingestion/pipeline.py
"""dlt ingestion pipeline: extract-load (dlt) + transform (Python).

Design
------
dlt owns extraction and the raw load. Each source is a dlt resource that yields
records into a raw table dlt creates and manages (schema, state, idempotent
merge on primary key). A single Python transform then maps the raw rows into the
hand-built `documents` / `chunks` domain schema, which carries a generated
tsvector column, a topic-union merge, and a downstream vector column that dlt's
schema inference should not manage. Embedding stays a separate follow-on step
(ingestion.embed), since it is an external paid API call, not an extract step.

This is deliberately dlt-for-ingestion + a Python transform rather than dlt+dbt:
the raw->refined mapping is procedural upsert logic with custom merge semantics
over two target tables, which a tested Python function expresses more directly
than a SQL modeling framework would.

Two-source ready: PubMed is the first resource. A CMS/CDC policy resource is
added the same way — one @dlt.resource per source, one mapper per source, both
converging on the shared `documents` / `chunks` schema.

Run the whole pipeline (extract -> load -> transform):
    uv run python -m ingestion.pipeline
Then embed:
    uv run python -m ingestion.embed
"""

from __future__ import annotations

import logging
import os

import dlt
from dlt.sources.credentials import ConnectionStringCredentials
from dotenv import load_dotenv

from ingestion.fetch_pubmed import collect
from ingestion.fetch_cms import collect as collect_cms
from ingestion.load import build_content, connect
from ingestion.chunking import split_text

from pathlib import Path
import json

PUBMED_SNAPSHOT = Path("data/pubmed.jsonl")
CMS_SNAPSHOT = Path("data/cms.jsonl")

load_dotenv()

log = logging.getLogger(__name__)

PIPELINE_NAME = "phea"
RAW_DATASET = "raw"


# ---------------------------------------------------------------- destination


def _pg_credentials() -> ConnectionStringCredentials:
    """Build dlt's Postgres credentials from the same env vars connect() uses,
    so the pipeline and the rest of the project share one configuration."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "phea")
    password = os.getenv("POSTGRES_PASSWORD", "phea")
    db = os.getenv("POSTGRES_DB", "phea")
    return ConnectionStringCredentials(
        f"postgresql://{user}:{password}@{host}:{port}/{db}"
    )


# ---------------------------------------------------------------- resources


@dlt.resource(
    name="pubmed_raw",
    write_disposition="merge",
    primary_key=("doc_id", "topic"),
)
def pubmed_raw(source_mode: str = "pinned"):
    """PubMed documents, one row per (doc_id, topic).

    source_mode:
      - "pinned": read the frozen snapshot at data/pubmed.jsonl (default).
        Deterministic and offline, so ingestion reproduces the exact corpus the
        evaluation (qrels, metrics, judge) was built against. This is the mode
        for development and for anyone reproducing the project.
      - "live": fetch fresh from the PubMed E-utilities API via collect().
        For scheduled corpus refreshes. A live refresh changes the corpus and
        therefore requires re-running evaluation; it is not for routine runs.

    The same two-mode contract will apply to the CMS/CDC resource, so a
    scheduled refresh can re-pull every source uniformly, while the default
    pinned mode keeps the evaluated corpus reproducible.
    """
    if source_mode == "live":
        records = collect()
    elif source_mode == "pinned":
        if not PUBMED_SNAPSHOT.exists():
            raise FileNotFoundError(
                f"{PUBMED_SNAPSHOT} not found. This is the pinned corpus "
                "snapshot the evaluation depends on. Run in live mode to "
                "create it, or restore the file."
            )
        records = (json.loads(line) for line in PUBMED_SNAPSHOT.open() if line.strip())
    else:
        raise ValueError(f"unknown source_mode {source_mode!r}; expected 'pinned' or 'live'")

    for rec in records:
        topics = rec.get("topics") or []
        if not topics:
            yield {**rec, "topic": None}
            continue
        for topic in topics:
            yield {**rec, "topic": topic}


@dlt.resource(
    name="cms_raw",
    write_disposition="merge",
    primary_key=("doc_id", "topic"),
)
def cms_raw(source_mode: str = "pinned"):
    """CMS Provider Data Catalog program/measure descriptions, one row per
    (doc_id, topic). Same pinned/live contract as pubmed_raw:

      - "pinned": read the frozen snapshot at data/cms.jsonl (default).
      - "live": fetch fresh from the CMS metastore via collect_cms().

    CMS descriptions carry source='cms' and doc_type='policy' so retrieval and
    evaluation can distinguish policy from peer-reviewed literature.
    """
    if source_mode == "live":
        records = collect_cms()
    elif source_mode == "pinned":
        if not CMS_SNAPSHOT.exists():
            raise FileNotFoundError(
                f"{CMS_SNAPSHOT} not found. This is the pinned CMS policy "
                "snapshot. Run `uv run python -m ingestion.fetch_cms` to create "
                "it, or restore the file."
            )
        records = (json.loads(line) for line in CMS_SNAPSHOT.open() if line.strip())
    else:
        raise ValueError(f"unknown source_mode {source_mode!r}; expected 'pinned' or 'live'")

    for rec in records:
        topics = rec.get("topics") or []
        if not topics:
            yield {**rec, "topic": None}
            continue
        for topic in topics:
            yield {**rec, "topic": topic}


# ---------------------------------------------------------------- transform


def _fetch_raw_documents(dataset: str, table_name: str = "pubmed_raw") -> dict[str, dict]:
    """Read a raw table and collapse to one record per doc_id, with topics
    unioned across the (doc_id, topic) rows dlt loaded."""
    table = f"{dataset}.{table_name}"
    by_doc: dict[str, dict] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table}")  # noqa: S608 - dataset is internal, not user input
        for row in cur.fetchall():
            doc_id = row["doc_id"]
            topic = row.get("topic")
            if doc_id not in by_doc:
                # Copy the row, start a fresh topic set.
                rec = dict(row)
                rec["topics"] = set()
                by_doc[doc_id] = rec
            if topic:
                by_doc[doc_id]["topics"].add(topic)
    # Normalise topic sets back to sorted lists.
    for rec in by_doc.values():
        rec["topics"] = sorted(rec["topics"])
    return by_doc


UPSERT_DOC = """
INSERT INTO documents (
    doc_id, source, topics, title, abstract, journal, doi, url,
    published_year, mesh_terms, publication_types, indexing_method, doc_type
)
VALUES (
    %(doc_id)s, %(source)s, %(topics)s, %(title)s, %(abstract)s,
    %(journal)s, %(doi)s, %(url)s, %(published_year)s,
    %(mesh_terms)s, %(publication_types)s, %(indexing_method)s, %(doc_type)s
)
ON CONFLICT (doc_id) DO UPDATE SET
    topics            = EXCLUDED.topics,
    title             = EXCLUDED.title,
    abstract          = EXCLUDED.abstract,
    journal           = EXCLUDED.journal,
    doi               = EXCLUDED.doi,
    url               = EXCLUDED.url,
    published_year    = EXCLUDED.published_year,
    mesh_terms        = EXCLUDED.mesh_terms,
    publication_types = EXCLUDED.publication_types,
    indexing_method   = EXCLUDED.indexing_method,
    doc_type          = EXCLUDED.doc_type
"""

UPSERT_CHUNK = """
INSERT INTO chunks (doc_id, chunk_index, content)
VALUES (%(doc_id)s, %(chunk_index)s, %(content)s)
ON CONFLICT (doc_id, chunk_index) DO UPDATE SET
    content = EXCLUDED.content
"""


def _doc_params(rec: dict) -> dict:
    """Map a raw record to the documents-table parameter set, filling defaults
    for any fields a source may not provide."""
    return {
        "doc_id": rec["doc_id"],
        "source": rec.get("source", "pubmed"),
        "topics": rec.get("topics", []),
        "title": rec.get("title"),
        "abstract": rec.get("abstract"),
        "journal": rec.get("journal"),
        "doi": rec.get("doi"),
        "url": rec.get("url"),
        "published_year": rec.get("published_year"),
        "mesh_terms": rec.get("mesh_terms", []),
        "publication_types": rec.get("publication_types", []),
        "indexing_method": rec.get("indexing_method"),
        "doc_type": rec.get("doc_type", "literature"),
    }


def transform_to_documents(dataset: str = RAW_DATASET) -> tuple[int, int]:
    """Map dlt's raw pubmed rows into documents + chunks. Topics are unioned in
    Python (explicit group-by) rather than via ON CONFLICT accumulation, so each
    document is written once with its full topic set. Returns (n_docs, n_chunks).
    """
    by_doc = _fetch_raw_documents(dataset)
    n_docs = n_chunks = 0
    with connect() as conn, conn.cursor() as cur:
        for rec in by_doc.values():
            cur.execute(UPSERT_DOC, _doc_params(rec))
            n_docs += 1
            cur.execute(
                UPSERT_CHUNK,
                {
                    "doc_id": rec["doc_id"],
                    "chunk_index": 0,
                    "content": build_content(rec),
                },
            )
            n_chunks += 1
        conn.commit()
    return n_docs, n_chunks


def transform_cms_to_documents(dataset: str = RAW_DATASET) -> tuple[int, int]:
    """Map dlt's raw CMS rows into documents + chunks. Unlike PubMed (one chunk
    per doc), CMS policy descriptions are split into multiple chunks via
    split_text when long enough, so retrieval returns a focused passage rather
    than a whole program description. Returns (n_docs, n_chunks)."""
    by_doc = _fetch_raw_documents(dataset, table_name="cms_raw")
    n_docs = n_chunks = 0
    with connect() as conn, conn.cursor() as cur:
        for rec in by_doc.values():
            rec.setdefault("doc_type", "policy")
            cur.execute(UPSERT_DOC, _doc_params(rec))
            n_docs += 1
            # Chunk the title+abstract content. split_text returns one chunk for
            # short text and several for long descriptions.
            content = build_content(rec)
            for idx, chunk in enumerate(split_text(content)):
                cur.execute(
                    UPSERT_CHUNK,
                    {"doc_id": rec["doc_id"], "chunk_index": idx, "content": chunk},
                )
                n_chunks += 1
        conn.commit()
    return n_docs, n_chunks


# ---------------------------------------------------------------- entrypoint


def run(source_mode: str = "pinned") -> None:
    pipeline = dlt.pipeline(
        pipeline_name=PIPELINE_NAME,
        destination=dlt.destinations.postgres(credentials=_pg_credentials()),
        dataset_name=RAW_DATASET,
    )

    log.info("extracting + loading pubmed raw via dlt (source_mode=%s)", source_mode)
    load_info = pipeline.run(pubmed_raw(source_mode=source_mode))
    log.info("dlt load (pubmed): %s", load_info)

    log.info("extracting + loading cms raw via dlt (source_mode=%s)", source_mode)
    cms_load_info = pipeline.run(cms_raw(source_mode=source_mode))
    log.info("dlt load (cms): %s", cms_load_info)

    log.info("transforming pubmed raw -> documents/chunks")
    n_docs, n_chunks = transform_to_documents(RAW_DATASET)
    log.info("pubmed documents upserted: %d", n_docs)
    log.info("pubmed chunks upserted: %d", n_chunks)

    log.info("transforming cms raw -> documents/chunks")
    n_docs_cms, n_chunks_cms = transform_cms_to_documents(RAW_DATASET)
    log.info("cms documents upserted: %d", n_docs_cms)
    log.info("cms chunks upserted: %d", n_chunks_cms)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM documents")
        total_docs = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM chunks")
        total_chunks = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM chunk_embeddings")
        total_embedded = cur.fetchone()["n"]
    log.info("totals — documents: %d, chunks: %d (%d embedded)",
             total_docs, total_chunks, total_embedded)
    log.info("next: uv run python -m ingestion.embed")


def main() -> None:
    import sys
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    mode = sys.argv[1] if len(sys.argv) > 1 else "pinned"
    run(source_mode=mode)


if __name__ == "__main__":
    main()