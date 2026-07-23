# ingestion/embed.py
"""Embed chunks and store vectors in chunk_embeddings.

    uv run python -m ingestion.embed
    uv run python -m ingestion.embed --model text-embedding-3-large
"""

import argparse
import logging
import os

from dotenv import load_dotenv
from openai import OpenAI
from pgvector.psycopg import register_vector
from tenacity import retry, stop_after_attempt, wait_exponential

from ingestion.load import connect

load_dotenv()

log = logging.getLogger(__name__)

DEFAULT_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
BATCH_SIZE = 100

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30))
def embed_batch(texts: list[str], model: str) -> list[list[float]]:
    resp = client.embeddings.create(
        input=texts,
        model=model,
        dimensions=DIMENSIONS,
    )
    return [d.embedding for d in resp.data]


def pending_chunks(conn, model: str) -> list[dict]:
    """Chunks with no embedding for this model."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.content
            FROM chunks c
            LEFT JOIN chunk_embeddings e
                   ON e.chunk_id = c.id AND e.model = %s
            WHERE e.chunk_id IS NULL
            ORDER BY c.id
            """,
            (model,),
        )
        return cur.fetchall()


def store(conn, rows: list[tuple[int, str, list[float]]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunk_embeddings (chunk_id, model, embedding)
            VALUES (%s, %s, %s)
            ON CONFLICT (chunk_id, model) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    created_at = now()
            """,
            rows,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with connect() as conn:
        register_vector(conn)

        pending = pending_chunks(conn, args.model)
        log.info("%d chunks to embed with %s", len(pending), args.model)

        if not pending:
            return

        for i in range(0, len(pending), BATCH_SIZE):
            batch = pending[i:i + BATCH_SIZE]
            vectors = embed_batch([r["content"] for r in batch], args.model)
            store(conn, [
                (r["id"], args.model, v)
                for r, v in zip(batch, vectors)
            ])
            conn.commit()
            log.info("embedded %d/%d", min(i + BATCH_SIZE, len(pending)), len(pending))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM chunk_embeddings WHERE model = %s",
                (args.model,),
            )
            log.info("total embeddings for %s: %d", args.model, cur.fetchone()["n"])


if __name__ == "__main__":
    main()