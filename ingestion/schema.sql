DROP TABLE IF EXISTS chunk_embeddings;
DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS documents;

CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    topics TEXT[] NOT NULL DEFAULT '{}',
    title TEXT,
    abstract TEXT,
    journal TEXT,
    doi TEXT,
    url TEXT,
    published_year INT,
    mesh_terms TEXT[] DEFAULT '{}',
    publication_types TEXT[] DEFAULT '{}',
    indexing_method TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (doc_id, chunk_index)
);

CREATE TABLE chunk_embeddings (
    chunk_id BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model)
);

CREATE INDEX chunks_tsv_idx ON chunks USING GIN (content_tsv);
CREATE INDEX documents_topics_idx ON documents USING GIN (topics);
CREATE INDEX documents_mesh_idx ON documents USING GIN (mesh_terms);
CREATE INDEX documents_year_idx ON documents (published_year);
CREATE INDEX documents_source_idx ON documents (source);