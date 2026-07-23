DROP TABLE IF EXISTS chunks;
DROP TABLE IF EXISTS documents;

CREATE TABLE documents (
    doc_id TEXT PRIMARY KEY,              -- PMID, or slug for non-PubMed
    source TEXT NOT NULL,                 -- 'pubmed' | 'cdc' | 'cms'
    topics TEXT[] NOT NULL DEFAULT '{}',  -- the five query labels
    title TEXT,
    abstract TEXT,
    journal TEXT,
    doi TEXT,
    url TEXT,
    published_year INT,
    mesh_terms TEXT[] DEFAULT '{}',
    publication_types TEXT[] DEFAULT '{}',
    indexing_method TEXT,                 -- Manual | Automated | Curated | NULL
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,                -- title + abstract text, what gets embedded
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding vector(1536),
    UNIQUE (doc_id, chunk_index)
);

CREATE INDEX chunks_tsv_idx ON chunks USING GIN (content_tsv);
CREATE INDEX documents_topics_idx ON documents USING GIN (topics);
CREATE INDEX documents_mesh_idx ON documents USING GIN (mesh_terms);
CREATE INDEX documents_year_idx ON documents (published_year);
CREATE INDEX documents_source_idx ON documents (source);