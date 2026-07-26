# monitoring/store.py
"""Persistence for query logs and user feedback.

Reuses the existing connect() from ingestion.load so there is one connection
path for the whole project. The app writes a query_log row per answer and a
feedback row per thumbs rating; the dashboard reads both.
"""

from __future__ import annotations

from ingestion.load import connect


def log_query(
    session_id: str,
    question: str,
    mode: str,
    answer: str,
    context_pmids: list[str],
    n_cited: int,
    n_valid_cited: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    multipart: bool | None = None,
    subquestions: list[str] | None = None,
) -> int:
    """Insert one query_log row, returning its id for later feedback linkage."""
    sql = """
        INSERT INTO query_log (
            session_id, question, mode, multipart, subquestions, answer,
            context_pmids, n_cited, n_valid_cited, prompt_tokens,
            completion_tokens, latency_ms
        ) VALUES (
            %(session_id)s, %(question)s, %(mode)s, %(multipart)s, %(subquestions)s,
            %(answer)s, %(context_pmids)s, %(n_cited)s, %(n_valid_cited)s,
            %(prompt_tokens)s, %(completion_tokens)s, %(latency_ms)s
        )
        RETURNING id
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, {
            "session_id": session_id,
            "question": question,
            "mode": mode,
            "multipart": multipart,
            "subquestions": subquestions or [],
            "answer": answer,
            "context_pmids": context_pmids,
            "n_cited": n_cited,
            "n_valid_cited": n_valid_cited,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
        })
        row = cur.fetchone()
        conn.commit()
        return row["id"] if isinstance(row, dict) else row[0]


def record_feedback(query_id: int, session_id: str, rating: int) -> None:
    """Upsert a thumbs rating for a query. rating is +1 or -1.

    One rating per query; re-rating overwrites, so a user can change their mind.
    """
    sql = """
        INSERT INTO feedback (query_id, session_id, rating)
        VALUES (%(query_id)s, %(session_id)s, %(rating)s)
        ON CONFLICT (query_id) DO UPDATE
            SET rating = EXCLUDED.rating, created_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, {"query_id": query_id, "session_id": session_id, "rating": rating})
        conn.commit()