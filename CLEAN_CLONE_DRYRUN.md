# Fresh checkout verification

This procedure verifies that the project can be brought up from nothing on a
machine that has never run it. Working from a fresh checkout in an isolated
directory is the only reliable way to catch setup gaps: a machine that already
has a populated database, cached models, and a filled-in `.env` will start the
application even when the documented steps are incomplete.

Run this in a scratch directory, not your working copy. Do not copy an existing
`.env` or any data across. The only value that should cross over is an OpenAI API
key, typed into a fresh `.env`.

## 0. Isolate

```
cd $(mktemp -d)                      # scratch directory
git clone <repository-url>
cd public-health-evidence-assistant
```

If you have run this project on this machine before, remove any leftover volumes
so the run starts from a genuinely empty state:

```
docker compose down -v
docker volume ls | grep -E 'postgres_data|langfuse_db_data|clickhouse_data|minio_data'
# expect no rows for this project's volumes before starting
```

## 1. Static pre-check

```
bash scripts/check_repro.sh
```

This validates the checkout in a few seconds without starting anything. If it
reports any FAIL, resolve it before continuing; there is no value in bringing up
the stack while a required file or setup step is missing. It must exit 0.

## 2. Environment

```
cp .env.example .env
# Edit .env: set OPENAI_API_KEY. Leave LANGFUSE_* blank for now; they are
# filled in during step 5.
```

Checkpoint: `.env` exists, contains a valid OpenAI key, and the POSTGRES_*
values are left at their defaults.

## 3. Bring up the stack

```
docker compose up -d
```

Checkpoint — wait for these before continuing:

```
docker compose ps
# postgres, langfuse, langfuse-worker, langfuse-clickhouse, langfuse-minio,
# langfuse-redis, and langfuse-db should be running/healthy.
# langfuse-minio-init is one-shot and should exit 0.
```

Confirm the object-storage bucket was created:

```
docker compose logs langfuse-minio-init | grep -i 'bucket ready'
```

## 4. Schema, ingest, embed, index

The `until pg_isready` gate matters on a cold start: Postgres creates the
required extensions during first-boot initialization, and the schema step
depends on them. Do not skip it.

```
until docker compose exec -T postgres pg_isready -U phea -d phea; do sleep 1; done
docker compose exec -T postgres psql -U phea -d phea < ingestion/schema.sql
docker compose exec -T postgres psql -U phea -d phea < ingestion/feedback_schema.sql
uv run python -m ingestion.pipeline
uv run python -m ingestion.embed
docker compose exec -T postgres psql -U phea -d phea < ingestion/indexes.sql
```

Checkpoints, in order:

- schema.sql runs with no "type vector does not exist" error, confirming the
  pgvector extension was created on first boot.
- The pipeline logs `documents upserted: 646`, `chunks upserted: 646`, and
  `totals — documents: 646, chunks: 646`. An upsert count of 0 means the
  transform step did not run and the corpus is empty.
- The embed step completes without an authentication error, confirming the
  OpenAI key is wired through. This step makes one embedding request per chunk.
- Verify the row counts directly rather than relying on the logs:

```
docker compose exec -T postgres psql -U phea -d phea -c \
 "select (select count(*) from documents) as docs,
         (select count(*) from chunks) as chunks,
         (select count(*) from chunk_embeddings) as embeddings;"
# expect 646 / 646 / 646
```

- Confirm the feedback tables exist:

```
docker compose exec -T postgres psql -U phea -d phea -c "\dt" | grep -E 'query_log|feedback'
```

## 5. Langfuse account and keys

Langfuse self-hosts in the stack but starts with an empty database, so the
account and API keys are created once per fresh volume.

Open http://localhost:3000, sign up (there is no mail server, so nothing is sent
or verified), create an organization and project, generate an API key pair, and
put the keys in `.env`:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
```

Then restart the application:

```
docker compose up -d app
```

The application degrades gracefully without Langfuse: queries still work and log
to Postgres, only tracing is skipped. This can be confirmed by proceeding with
the keys left blank and checking that the application still answers.

## 6. Exercise the application

Open http://localhost:8501.

- Ask an in-corpus question (for example, "What interventions reduce 30-day
  readmissions for heart failure patients?"). Confirm an answer returns, cited
  PMIDs render as PubMed links, and they resolve to real corpus papers.
- The first query may pause while the reranker model loads if it was not cached
  at build time; confirm it loads rather than erroring.
- Switch the mode selector to `no_retrieval`, ask the same question, and confirm
  cited PMIDs are flagged as not in the corpus. This demonstrates the
  citation-validity contrast between grounded and ungrounded modes.
- Submit one positive and one negative feedback rating. Neither should error.
- Open the Monitoring page and confirm all six charts render, populated by the
  queries and feedback just recorded.
- If Langfuse keys were configured, confirm the trace appears at
  http://localhost:3000. This requires both the langfuse and langfuse-worker
  containers; without the worker, traces are uploaded to object storage but
  never processed into the analytics store.

## 7. Tear down and confirm persistence (optional)

```
docker compose down          # keep volumes
docker compose up -d
```

Confirm the application still answers without re-ingesting, verifying that data
persisted in the volumes. To repeat the full cold-start path, run
`docker compose down -v` and restart from step 3; note that this wipes the
Langfuse database and its account and keys.
