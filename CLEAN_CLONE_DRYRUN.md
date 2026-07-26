# Clean-clone dry run

The reproducibility rubric point is scored by a peer reviewer who clones your
repo at a commit hash and follows the README from nothing. Your dev machine
passes trivially because its database is populated, its Langfuse account exists,
the reranker is cached, and `.env` holds real keys. None of that exists for the
reviewer. This protocol reproduces the reviewer's starting state so you find the
gaps before they do.

Run it in a throwaway directory, not your working copy. Do not copy your `.env`
or any data across. The only thing that should cross the boundary is your OpenAI
key, typed into a fresh `.env`.

## 0. Isolate

```
cd $(mktemp -d)                      # fresh scratch dir, nothing from your repo
git clone https://github.com/rdanielsstat/public-health-evidence-assistant.git
cd public-health-evidence-assistant
git reset --hard <commit-hash>       # the exact hash you will submit
```

Confirm you are NOT reusing old Docker volumes from a previous run — that is the
most common way a "clean" run silently isn't:

```
docker compose down -v               # only if you have run this project before
docker volume ls | grep -E 'postgres_data|langfuse_db_data|clickhouse_data|minio_data'
# expect no rows for THIS project's volumes before you start
```

## 1. Static pre-check (30 seconds, catches the cheap failures first)

```
bash scripts/check_repro.sh
```

If this reports any FAIL, stop and fix the repo — there is no point standing up
the stack when a committed file or README step is missing. It must exit 0.

## 2. Environment

```
cp .env.example .env
# edit .env: set OPENAI_API_KEY to a real key. Leave LANGFUSE_* blank.
```

Checkpoint: `.env` exists, has your OpenAI key, POSTGRES_* left at phea defaults.

## 3. Bring up the stack

```
docker compose up -d
```

Checkpoint — wait for these before continuing:

```
docker compose ps
# postgres, langfuse, langfuse-worker, langfuse-clickhouse, langfuse-minio,
# langfuse-redis, langfuse-db should be "running"/"healthy".
# langfuse-minio-init should have exited 0 (it is one-shot).
```

Watch specifically for: postgres reaching healthy (the schema step depends on
it), and langfuse-minio-init actually creating the bucket:

```
docker compose logs langfuse-minio-init | grep -i 'bucket ready'
```

## 4. Schema + ingest + embed + index

Follow the README exactly. The `until pg_isready` gate matters on a cold start;
do not skip it.

```
until docker compose exec -T postgres pg_isready -U phea -d phea; do sleep 1; done
docker compose exec -T postgres psql -U phea -d phea < ingestion/schema.sql
docker compose exec -T postgres psql -U phea -d phea < ingestion/feedback_schema.sql
uv run python -m ingestion.pipeline
uv run python -m ingestion.embed
docker compose exec -T postgres psql -U phea -d phea < ingestion/indexes.sql
```

Checkpoints, in order:

- schema.sql runs with no "type vector does not exist" error (proves init-db.sql
  created the extension on first boot).
- pipeline logs `documents upserted: 646`, `chunks upserted: 646`,
  `totals — documents: 646, chunks: 646`. If either upsert is 0, the transform
  step didn't run — the corpus is empty and everything downstream is hollow.
- embed completes without an OpenAI auth error (proves your key is wired through).
- Verify counts directly rather than trusting logs:

```
docker compose exec -T postgres psql -U phea -d phea -c \
 "select (select count(*) from documents) as docs,
         (select count(*) from chunks) as chunks,
         (select count(*) from chunk_embeddings) as embeddings;"
# expect 646 / 646 / 646
```

- feedback tables exist (the step the README used to omit):

```
docker compose exec -T postgres psql -U phea -d phea -c "\dt" | grep -E 'query_log|feedback'
```

## 5. Langfuse account + keys (the one manual, un-scriptable step)

Open http://localhost:3000, sign up (no mail server, nothing is verified),
create org/project, generate an API key pair, paste `pk-lf-...` / `sk-lf-...`
into `.env`, then:

```
docker compose up -d app
```

Checkpoint: this is the step most likely to trip a reviewer because it's manual
and sits mid-sequence. Re-read your own README section 5 as if you'd never seen
it. Is every click named? A reviewer who can't get keys still gets a working app
(tracing degrades gracefully) — confirm that's true by proceeding even if you
deliberately leave keys blank once, to test the graceful-degradation claim.

## 6. Exercise the app (this is what "it works" means)

Open http://localhost:8501.

- Ask one in-corpus question (e.g. "What interventions reduce 30-day
  readmissions for heart failure patients?"). Confirm: an answer returns, cited
  PMIDs render as PubMed links, and they resolve to real corpus papers.
- First query may pause while the reranker loads if the image didn't pre-cache;
  confirm it does load rather than erroring.
- Switch mode to `no_retrieval` in the sidebar, ask the same question, confirm
  cited PMIDs are flagged "not in corpus" (this is your citation-validity story
  made live).
- Give a thumbs up and a thumbs down. No error.
- Open the Monitoring page. Confirm all six charts render with your just-created
  query/feedback rows (this is the check that would have failed without
  feedback_schema.sql).
- If you added Langfuse keys: confirm the trace appears at localhost:3000
  (needs BOTH langfuse and langfuse-worker; without the worker, traces upload to
  MinIO but never reach the UI).

## 7. Record the evidence

Screenshot the app answer with citations, the Monitoring dashboard, and a
Langfuse trace. Drop them in the README (the rubric explicitly rewards
screenshots). Note the commit hash you validated — that is the hash you submit.

## 8. Tear down and confirm idempotency (optional but worth it)

```
docker compose down          # keep volumes
docker compose up -d
```

Confirm the app still works without re-ingesting (data persisted in volumes).
Then, only if you want to prove the full cold path once more, `down -v` and
repeat from step 3 — but remember that wipes the Langfuse account and keys.

---

### What a passing run proves for the rubric

- Instructions clear and complete: every step ran as written, in order.
- Dataset accessible: 646/646/646 came from the committed snapshot, no network
  fetch of the corpus.
- Easy to run and it works: a live answer with valid citations, dashboard, and
  (optionally) a trace.
- Versions pinned: `uv sync --frozen` installed from the committed lockfile.
