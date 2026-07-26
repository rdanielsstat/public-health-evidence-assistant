-- Feedback and query logging for the Streamlit app and monitoring dashboard.
-- Applied once, after the base schema, like indexes.sql.

CREATE TABLE IF NOT EXISTS query_log (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    question      TEXT NOT NULL,
    mode          TEXT NOT NULL,              -- 'agent_router' | 'dense_only' | ...
    multipart     BOOLEAN,                    -- router decision, null for non-agent modes
    subquestions  TEXT[] DEFAULT '{}',
    answer        TEXT NOT NULL,
    context_pmids TEXT[] DEFAULT '{}',
    n_cited       INT DEFAULT 0,
    n_valid_cited INT DEFAULT 0,              -- cited PMIDs present in documents
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    latency_ms    INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    query_id    BIGINT REFERENCES query_log(id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    rating      SMALLINT NOT NULL,           -- +1 thumbs up, -1 thumbs down
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (query_id)                         -- one rating per answer; upsert to change
);

CREATE INDEX IF NOT EXISTS query_log_created_idx ON query_log (created_at);
CREATE INDEX IF NOT EXISTS query_log_mode_idx ON query_log (mode);
CREATE INDEX IF NOT EXISTS feedback_rating_idx ON feedback (rating);