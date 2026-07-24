-- Applied after ingestion.embed populates chunk_embeddings.
-- HNSW builds its graph from data present at creation time, so this runs
-- after the vectors exist rather than as part of the base schema.

CREATE INDEX IF NOT EXISTS chunk_embeddings_hnsw_idx
    ON chunk_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
