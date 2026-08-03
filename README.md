# Healthcare Quality & Safety Evidence Assistant

A retrieval-augmented generation (RAG) and agent system that answers questions
about hospital quality and safety, drawing on peer-reviewed research and federal
measure data. When an answer draws on a retrieved document, it includes a
citation and a link back to the source so the answer can be verified.

The knowledge base combines two sources:

- **Peer-reviewed PubMed literature** (titles and abstracts) on quality, safety,
  readmissions, infections, and inpatient mortality.
- **CMS Provider Data Catalog** dataset descriptions covering federal quality
  and safety measures and programs, such as the Hospital Readmissions Reduction
  Program, the Hospital-Acquired Condition Reduction Program, and Hospital
  Value-Based Purchasing.

## Problem

Clinicians, hospital quality teams, and health policy analysts need to answer
questions that span different bodies of knowledge living in different places:

- **What does the evidence say?** Peer-reviewed studies on which interventions
  reduce readmissions, infections, and inpatient mortality.
- **How does CMS measure and track it?** Federal programs and measures, such as
  the Hospital Readmissions Reduction Program, that score hospitals on these
  same outcomes and tie them to reimbursement.

Answering a question like *"what interventions reduce 30-day readmissions, and
how does CMS measure hospital performance on them?"* currently means searching
PubMed, then separately browsing the CMS Provider Data Catalog, then reconciling
the two by hand.

This project builds a single interface over both sources. When an answer relies
on a retrieved document, it cites that document and links back to it, so answers
can be verified against the evidence.

### Example questions

- What interventions reduce 30-day readmissions for heart failure patients?
- Which infection prevention practices have the strongest evidence in ICU
  settings?
- What are the strongest predictors of inpatient mortality after sepsis?
- How do healthcare-associated infections affect hospital mortality rates?
- Has evidence on hand hygiene compliance interventions changed since 2020?

## Quick start

Requires Docker and [uv](https://docs.astral.sh/uv/). Everything runs locally.

```bash
# 1. Copy and fill environment variables
cp .env.example .env
#    Set OPENAI_API_KEY. POSTGRES_* default to phea/phea/phea.
#    Leave LANGFUSE_* blank for now; you fill them in step 5.

# 2. Start the stack (Postgres, app, Langfuse web + worker, ClickHouse,
#    MinIO, Redis, and the MinIO bucket-init)
docker compose up -d

# 3. Wait for Postgres to finish first-boot init (it creates the pgvector and
#    pg_trgm extensions on first start), then create the schemas. The schema
#    step needs the vector extension, so don't run it before Postgres is ready.
until docker compose exec -T postgres pg_isready -U phea -d phea; do sleep 1; done
docker compose exec -T postgres psql -U phea -d phea < ingestion/schema.sql
docker compose exec -T postgres psql -U phea -d phea < ingestion/feedback_schema.sql

# 4. Ingest the corpus (dlt load + transform), then embed, then build the
#    vector index (HNSW builds from data present at creation time, so it is
#    applied after embedding)
uv run python -m ingestion.pipeline
uv run python -m ingestion.embed
docker compose exec -T postgres psql -U phea -d phea < ingestion/indexes.sql

# 5. Set up Langfuse (one-time; see "Tracing" below), put the keys in .env,
#    then restart the app
docker compose up -d app

# 6. Open the app
open http://localhost:8501
```

Ingestion reads pinned corpus snapshots (`data/pubmed.jsonl` and
`data/cms.jsonl`), so step 4 is deterministic and reproduces the exact corpus
the evaluation was built against.

## Knowledge base

The corpus is deliberately narrow: inpatient healthcare quality and safety.
This keeps the knowledge base coherent enough that retrieval quality can be
measured meaningfully, while covering enough overlapping subtopics that
questions genuinely require synthesis across documents.

**Peer-reviewed literature (PubMed)** — abstracts retrieved via the NCBI
E-utilities API, anchored on five MeSH terms:

| Topic                            | MeSH term                         |
| -------------------------------- | --------------------------------- |
| Hospital readmissions            | `Patient Readmission`             |
| Healthcare-associated infections | `Cross Infection`                 |
| Patient safety                   | `Patient Safety`                  |
| Hospital mortality               | `Hospital Mortality`              |
| Inpatient quality                | `Quality Indicators, Health Care` |

These MeSH terms anchor the queries but are not used alone; see
[Query strategy](#query-strategy) below for the full formulations. All queries
are filtered to English-language records with abstracts, published 2018 or
later.

The five topics overlap conceptually. Readmissions, infections, and mortality
are all quality indicators, and a substantive question usually touches more
than one. This is what makes retrieval strategy matter: a question spanning two
topics must retrieve from documents that arrived under different queries, and
naive keyword search retrieves documents that share vocabulary rather than
documents that answer the question.

### Query strategy

Each topic is retrieved by a query that unions its MeSH term with
title/abstract phrase matches:

```
("Patient Readmission"[MeSH Terms]
 OR "patient readmission"[tiab]
 OR "hospital readmission*"[tiab]
 OR "30-day readmission*"[tiab])
AND ("2018"[PDAT] : "2026"[PDAT]) AND english[lang] AND hasabstract
```

MeSH terms are human-assigned by NLM indexers and are precise, but text
matching contributes on-topic papers that MeSH-only relevance ranking places
below the retrieval cutoff. Comparing the two strategies on the readmissions
topic, 173 of the top 200 union results were absent from the MeSH-only top 200,
and manual review confirmed they were squarely on-topic.

The union formulation was validated per topic rather than applied uniformly.
Four topics have unambiguous text phrases ("30-day readmission," "nosocomial
infection," "in-hospital mortality"). Quality does not: every phrase containing
"quality" is either too generic ("quality of care," which appears in the
discussion section of most health services research) or names an adjacent field
("quality improvement," which retrieves the QI-methodology literature rather
than quality measurement). Quality's text terms are therefore restricted to
titles and to measurement-specific phrasing — `"quality indicator*"`,
`"quality measure*"`, `"performance measure*"` — which recovers the intended
concept. This is a case where curated indexing outperforms text search, and the
query reflects that.

### Corpus characteristics

646 documents, evenly distributed across the five topics:

| Topic        | Documents |
| ------------ | --------- |
| safety       | 134       |
| readmissions | 130       |
| quality      | 129       |
| infections   | 128       |
| mortality    | 125       |

No document appears under more than one topic. The five queries are distinct
enough that relevance ranking produces disjoint top-N slices, even though the
underlying concepts overlap. Cross-topic questions remain answerable — "which
discharge interventions reduce both readmissions and post-discharge mortality"
is answered by retrieving documents that arrived under *different* topics, not
documents labeled with both. Ingestion merges topic labels when a PMID is
returned by multiple queries, so the schema supports overlap if sampling
changes.

Publication years span 2018-2026, weighted toward recent work:

| Year | Documents |
| ---- | --------- |
| 2018 | 51        |
| 2019 | 63        |
| 2020 | 71        |
| 2021 | 72        |
| 2022 | 121       |
| 2023 | 109       |
| 2024 | 90        |
| 2025 | 55        |
| 2026 | 14        |

The 2025-2026 tail is thinner than publication volume alone would suggest.
MeSH indexing lags publication by several months, so the most recent papers are
reachable only through text matching. The union query partially compensates,
but a corpus anchored on curated indexing will always under-represent the last
two quarters. This is a known limitation rather than a defect.

Every document carries MeSH terms and at least one publication type. 174
documents are tagged as reviews, including 75 systematic reviews and 30
meta-analyses; PubMed applies these tags in combination, so the categories
overlap rather than sum. The remainder is primary research, giving the corpus a
workable mix of evidence synthesis and original studies.

Of 675 records fetched, 29 were excluded during ingestion:

| Reason                    | Count |
| ------------------------- | ----- |
| Abstract under 30 words   | 15    |
| Excluded publication type | 13    |
| No abstract text          | 1     |

Short abstracts are placeholder stubs ("According to this study.") and
editorial teasers rather than research summaries; they would embed to noise and
pollute retrieval results. Excluded publication types are editorials, comments,
letters, and news items — with the exception of records that also carry a
substantive type, since research communications are sometimes published in
letter format.

### Chunking

Chunk size was determined empirically rather than by default. Across the
ingested corpus, PubMed abstracts are short and tightly bounded:

| Percentile | Words |
| ---------- | ----- |
| min        | 32    |
| median     | 215   |
| 95th       | 309   |
| max        | 399   |

Every abstract fits within a single embedding without truncation, so PubMed
documents are indexed whole — title prepended to abstract, one chunk per
document. Splitting a 215-word abstract would fragment the context that makes
it retrievable, and the standard 500-token chunking default would be a no-op on
this corpus anyway.

Titles are prepended to abstract text before embedding. Titles carry dense,
query-like phrasing ("Implementing a Discharge Follow-up Phone Call Program
Reduces Readmission Rates") that overlaps directly with how users phrase
questions. This choice is a deliberate design decision rather than a measured
one — title-prepended versus abstract-only was not separately ablated, since
the corpus is small and every title is short, dense, and topical.

The chunking layer is retained for policy and guidance documents (CDC, CMS),
which are substantially longer and do require splitting.

### Ingestion pipeline

Ingestion is a [dlt](https://dlthub.com/) pipeline. dlt owns raw extraction,
load, state, and schema (into a `raw` dataset); a Python transform maps raw rows
into the `documents` and `chunks` tables (topic-union merge, title+abstract
content). Embedding is a separate follow-on step, since it is an external paid
API call rather than an extract step.

```bash
uv run python -m ingestion.pipeline      # dlt load + transform (pinned corpus)
uv run python -m ingestion.embed         # OpenAI embeddings for each chunk
```

The pipeline reads the pinned corpus snapshots at `data/pubmed.jsonl` and
`data/cms.jsonl` by default, so ingestion is deterministic and reproduces the
exact corpus the evaluation was built against. A live refresh from the PubMed
E-utilities API is available for scheduled re-pulls:

```bash
uv run python -m ingestion.pipeline live
```

A live refresh from a relevance-ranked API returns a slightly different corpus
each time, which would drift the corpus out of lockstep with the evaluation's
relevance judgments; a live refresh therefore requires re-running the evaluation
and is not used for routine setup. The same pinned/live contract is designed to
apply to the planned policy corpus, so a scheduled refresh can re-pull every
source uniformly while pinned mode keeps the evaluated corpus reproducible.

**Design note.** The pipeline uses dlt for ingestion and a Python transform for
the raw→refined mapping, rather than dlt + dbt. The mapping is procedural upsert
logic with custom merge semantics (topic union) over two target tables carrying
a generated tsvector and a downstream vector column; a tested Python function
expresses this more directly than a SQL modeling framework, which would add a
second transform tool for a two-table mapping. dlt manages extraction, load
state, schema versioning, and idempotent merge — visible as the `_dlt_*` tables
and the normalized child tables (`pubmed_raw__topics`, etc.) in the `raw`
dataset.

## Retrieval

### Lexical search

Postgres full-text search over a generated `tsvector` column with a GIN index,
ranked by `ts_rank_cd`.

Query terms are OR-joined rather than AND-joined. Both `plainto_tsquery` and
`websearch_to_tsquery` produce AND-joined queries for bare phrases, requiring
every term to appear — which returned 3 results for a 5-term query against a
646-document corpus. Building the tsquery explicitly with `|` separators lets
partial matches rank rather than filter.

### Dense search

pgvector cosine distance over `text-embedding-3-small` at 1536 dimensions.
Embeddings are stored in a separate `chunk_embeddings` table keyed by
`(chunk_id, model)`, so multiple embedding models can be compared without
schema changes.

An HNSW index is created on `chunk_embeddings` for cosine distance. At the
current corpus size the planner correctly ignores it in favour of a sequential
scan — `EXPLAIN ANALYZE` shows 5.8ms — so it provides no benefit today. It is
included because it is the correct structure for the corpus to grow into, and
because the index operator class must match the distance operator used at query
time (`vector_cosine_ops` with `<=>`); a mismatch silently disables the index.

### Hybrid search

Lexical and dense retrieval are fused with Reciprocal Rank Fusion (RRF).
RRF ignores the raw scores from each method, which live on different scales
(`ts_rank_cd` and cosine distance aren't comparable), and combines only the
rank positions: each document scores `1 / (k + rank)` per list it appears in,
summed across lists, with `k = 60`. A document found by both searches collects
a contribution from each; one found by a single search collects one. Ranks
start at 1 in this implementation, which is equivalent to the rank-0
convention up to a constant shift absorbed by `k`.

`search_hybrid()` in `retrieval/fusion.py` shares the signature of the other
two retrievers, drawing 50 candidates from each before fusing.

### Reranking

Cross-encoder reranking (`retrieval/rerank.py`, `BAAI/bge-reranker-v2-m3`, run
locally, no API) takes the top 30 hybrid RRF candidates and reorders them by
scoring each (query, document) pair jointly, replacing the fusion score with a
cross-encoder relevance score. See [Retrieval metrics](#retrieval-metrics) for
the measured lift.

### Query router (agent)

Dense and hybrid retrieval collapse multi-part questions onto a single topic:
q14 ("which discharge interventions reduce both readmissions and post-discharge
mortality") retrieves readmissions documents and never surfaces the mortality
literature. The query router (`agents/router.py`, a LangGraph graph) adds a
decision node that detects multi-part questions, decomposes them into
per-topic sub-questions, retrieves for each with `hybrid_rerank`, and merges the
results so the generator sees evidence from every part. Single-part questions
pass straight through to the same retrieval-and-generation path. This is the one
component where control flow is decided by the model rather than fixed in
advance, and the decomposition doubles as query rewriting.

Routing is evaluated (`evaluation/router_eval.py`) against a ground-truth label
taken from the question annotations: a question is multi-part when its
`topics_intent` covers more topics than single-query retrieval actually
surfaced. Two policy questions (q19, q20) were initially mislabeled multi-part;
their `topics_intent` expressed that the evidence spans two literatures, which
is not the same as the question asking two things, and they were corrected to
single-part.

An initial router prompt caught only explicit conjunctions ("both X and Y") and
missed relational two-topic questions ("how do infections affect mortality"),
giving recall 1/3 on the genuine multi-part questions. One prompt revision, made
on principle rather than by tuning — naming the relational pattern and excluding
single questions whose evidence merely spans areas — raised recall to 3/3:

| metric               | value                                    |
|----------------------|------------------------------------------|
| routing accuracy     | 0.958 (23/24)                            |
| recall (multi-part)  | 1.000 (3/3)                              |
| precision            | 0.750 (one over-split: q10)              |

The one false positive (q10, "staffing shortages affect safety and inpatient
outcomes") is an arguable disagreement rather than a clear error, and was left
rather than tuned away on the same 24 questions.

On the decomposed questions, a retrieval-collapse check confirms the merged
context now covers the intended topics wherever the corpus contains them: q15
(infections + mortality) and q18 (quality + mortality) both reach full topic
coverage, versus the single-topic collapse the same questions showed before. q14
remains partial — correct decomposition still retrieves only readmissions
documents, because the corpus lacks papers linking discharge interventions to
mortality. This distinguishes a routing failure (retrieving one topic when asked
for two) from a corpus-coverage limit (retrieving for both, but the evidence for
one does not exist), and q14 is the latter: the router did its job and the gap
is in the data, which the grounded generator correctly reports rather than
papering over.

## Evaluation

> **Note on corpus version.** The evaluation below was built and run against the
> PubMed-only corpus (646 documents). The CMS source described above is a later
> addition. The reported figures therefore characterize the PubMed retrieval
> pipeline, which is unchanged by the CMS addition.

### Relevance judgments

Retrieval candidates were pooled from both methods (top 10 each, union) and
graded 0/1/2 by `gpt-4o-mini`. This produced 431 judgments across 24
questions: 170 not relevant, 183 partially relevant, 78 highly relevant.

Grader calibration was checked against manual judgment on a blind stratified
sample of 36 pairs, 12 from each grade, with the automated grades withheld:

| Measure                              | Result       |
| ------------------------------------ | ------------ |
| Exact agreement                      | 24/36 (67%)  |
| Within one grade                     | 35/36 (97%)  |
| Quadratic weighted kappa             | 0.71         |
| Manual grade stricter than model     | 8            |
| Manual grade more lenient than model | 4            |

Agreement by grade: 92% on not-relevant, 42% on partially relevant, 67% on
highly relevant. The middle grade is the least reliable, which is expected —
partial relevance is the fuzziest boundary — and the least consequential,
since recall is thresholded at either grade ≥ 1 or grade ≥ 2 and borderline
cases fall on both sides. Only one pair differed by two grades.

An earlier calibration attempt graded from titles alone and produced 64%
agreement with a strictly one-directional bias: 13 manual downgrades and zero
upgrades. Repeating the exercise with full abstracts visible eliminated the
bias (8 down, 4 up), indicating the apparent leniency was an artifact of
grading with less information than the automated grader had, not a property of
the grader.

### Pipeline variants

Variants are scored on an identical question set:

| Variant         | Retrieval                                    |
| --------------- | -------------------------------------------- |
| `no_retrieval`  | none — LLM answers from parametric knowledge |
| `lexical_only`  | Postgres full-text, `ts_rank_cd`             |
| `dense_only`    | pgvector cosine, `text-embedding-3-small`    |
| `hybrid_rrf`    | reciprocal rank fusion over both             |
| `hybrid_rerank` | RRF candidates reranked by cross-encoder     |

The deployed system uses the query router (agent), which wraps `hybrid_rerank`
with multi-part decomposition.

### Retrieval metrics

Recall@5, Recall@10, MRR, and NDCG@10 for each retriever against the 431
graded judgments, over the 23 in-scope questions (out-of-scope q23 is
excluded, having no relevant document to recall). Computed by
`evaluation/retrieval_metrics.py`.

| retriever  | recall@5 | recall@10 | mrr   | ndcg@10 |
|------------|----------|-----------|-------|---------|
| lexical    | 0.219    | 0.417     | 0.774 | 0.479   |
| dense      | 0.439    | 0.730     | 0.978 | 0.828   |
| hybrid_rrf | 0.320    | 0.548     | 0.896 | 0.681   |

Dense retrieval is the strongest single method by a wide margin, placing a
relevant document at rank 1 on nearly every question (0.978 MRR). Equal-weight
RRF underperforms dense on every metric. The cause is retriever quality
asymmetry: RRF treats agreement between the two ranked lists as signal, which
holds when both inputs are independently informative, but here dense is
near-oracle while lexical has poor mid-rank precision. Documents both
retrievers surface, even at mediocre ranks, collect two contributions and
outrank a document dense alone ranks first with high confidence. On the stroke
quality-indicator question, dense's rank-1 relevant document is pushed out of
the fused top 10 entirely by lexical's co-occurring lower-value results.

Hybrid search is implemented and evaluated per the rubric; the measured finding
is that on this corpus it does not improve over the strongest single retriever.
A stronger lexical ranker (BM25 via a Postgres extension such as
VectorChord-BM25) would narrow the quality gap and is the natural next step if
hybrid is to be made competitive; it is noted as future work rather than built.

Grade-2 relevance (highly relevant documents only, 21 in-scope questions):

| retriever     | recall@5 | recall@10 | mrr   | ndcg@10 | judged_cov |
|---------------|----------|-----------|-------|---------|------------|
| lexical       | 0.359    | 0.463     | 0.552 | 0.509   | 1.000      |
| dense         | 0.625    | 0.829     | 0.804 | 0.832   | 0.995      |
| hybrid_rrf    | 0.501    | 0.770     | 0.695 | 0.711   | 0.833      |
| hybrid_rerank | 0.724    | 0.851     | 0.933 | 0.770   | 0.695      |

Reranking is where fusion pays off. On highly relevant documents it beats dense
on the two metrics a reranker exists to improve: Recall@5 (0.724 vs 0.625, more
grade-2 documents in the top 5) and MRR (0.933 vs 0.804, the first grade-2
document sits almost at rank 1). Dense retains a small edge on NDCG@10
(0.832 vs 0.770) and ties on Recall@10. This split is the expected
cross-encoder signature: the reranker sharpens the head of the ranking rather
than the whole list, since it only reorders the 30 candidates hybrid supplies
and inherits hybrid's weaker tail below rank 5. For a generator that reads the
top few documents, Recall@5 and MRR are the operative metrics, and reranking
wins both.

These figures are a lower bound. `hybrid_rerank` has the lowest judged coverage
of any method (0.695), meaning roughly a third of what it ranks was never in the
pooled judgment set and is scored as non-relevant by default. The reranker
promotes documents the two first-stage retrievers ranked too low to enter the
pool, some of which are likely relevant but ungraded, so its true performance is
at least this good. This is a limitation of pool-based evaluation, not of the
method: the pool was built from first-stage top-10 results and cannot fully
credit a reranker that reaches beyond it.

Taken together: dense is a strong single-retriever baseline; equal-weight RRF
underperforms it because the two retrievers are too unequal in quality for rank
fusion's independence assumption to hold; and cross-encoder reranking of the
fused candidates recovers the top of the ranking, beating dense on grade-2
Recall@5 and MRR. `hybrid_rerank` is the retriever the answer generator uses
(via the query router).

### LLM evaluation

Most of this corpus predates current model training cutoffs, so a language
model can produce a fluent answer to many of these questions without any
retrieval at all. The `no_retrieval` baseline tests whether the retrieval
pipeline adds value rather than assuming it does.

Each of the five pipeline variants is scored on three dimensions by
`evaluation/judge.py`, over the 24-question set:

- **Groundedness** (LLM-judged, 0–2): are the answer's claims supported by the
  retrieved evidence? Only meaningful when there is context, so it is scored for
  the four retrieval variants and not for `no_retrieval`.
- **Completeness** (LLM-judged, 0–2): does the answer address the question? The
  judge is instructed to ignore formatting and sourcing and to score a
  well-formatted thin answer below a plain thorough one.
- **Citation validity** (mechanical, not judged): the fraction of cited PMIDs
  that exist in the corpus. PMIDs are extracted with a regex and tested for
  membership in the `documents` table.

The judge is `gpt-4o` at temperature 0. Generation and judging both retry with
exponential backoff on rate limits, and results are written per-record so an
interrupted run resumes without re-spending.

| variant       | n  | groundedness | completeness | citation_validity | answers w/ invalid cite |
|---------------|----|--------------|--------------|-------------------|-------------------------|
| no_retrieval  | 24 | n/a          | 2.000        | 0.000             | 24                      |
| lexical_only  | 24 | 2.000        | 0.917        | 1.000             | 0                       |
| dense_only    | 24 | 2.000        | 1.667        | 1.000             | 0                       |
| hybrid_rrf    | 24 | 2.000        | 1.500        | 1.000             | 0                       |
| hybrid_rerank | 24 | 1.958        | 1.750        | 1.000             | 0                       |

Two dimensions carry decisive signal. **Groundedness is ~2.0 across every
retrieval variant**: once any real corpus document is in context, the generator
grounds its answer in it, and grounding is robust to which retriever supplied
the context. **Citation validity separates cleanly**: every grounded variant
cites only real corpus PMIDs (1.000), while `no_retrieval` fabricates its
sources on all 24 questions (0.000). Spot-checking those fabricated identifiers
found them resolving to real but unrelated PubMed papers — a failure that looks
checkable and passes a casual glance, which is more dangerous than an invented
number. (An early spot check on the heart-failure readmissions question found
all seven cited PMIDs wrong: six valid identifiers pointing at unrelated papers
— an immunotoxin trial, a pediatric hepatoblastoma study, a paper on Rac1
signalling in rat inflammatory pain — and one that does not resolve, with one
paper cited twice under two different author attributions.)

This is why answer quality and citation validity are scored as separate
dimensions and never combined. `no_retrieval` scores the *highest* completeness
(2.000) while failing citation validity completely. A single blended quality
score would therefore rank the variant that fabricates every source above the
grounded pipeline.

The completeness column should not be read as a ranking of retrievers. It is
confounded in two ways. First, out-of-scope questions (e.g. q23, q24) invert the
signal: the grounded pipeline correctly retrieves nothing and declines to
answer, scoring completeness 0, while `no_retrieval` scores 2 for confidently
answering a question it should refuse — so part of the baseline's completeness
advantage is precisely its willingness to answer out of scope. Second, a single
unreferenced completeness integer over 24 questions is a coarser instrument than
the retrieval metrics (431 graded judgments); the ~0.25 spread among the four
grounded variants is within its noise. Retriever quality is ranked by the
retrieval metrics above, not by this column; the judge's role here is to
evaluate generation, and its trustworthy result is about grounding and
citation validity.

## Interface

A [Streamlit](https://streamlit.io/) app (`app/main.py`). The main page answers
questions and renders each cited PMID as a PubMed link, so grounded citations
are verifiable in one click. A sidebar selector switches between the agent
(default) and any single pipeline variant, which makes the citation-validity
contrast visible live: grounded modes resolve to real corpus papers, the
no-retrieval baseline resolves to fabricated ones. Sessions are capped to bound
API cost.

## Monitoring

Two layers.

**In-app dashboard.** The app logs every query to Postgres (`query_log`) and
records thumbs feedback (`feedback`). The Monitoring page (sidebar) renders six
charts from those tables: queries over time, queries by mode, citation validity
by mode (grounded modes ~1.0, `no_retrieval` ~0.0), feedback split, median
latency by mode (the agent is slower — routing plus per-subquestion retrieval),
and token usage over time.

**Tracing.** Each query is traced to self-hosted Langfuse (`v3`), which records
input, output, latency, and per-query model cost computed from token counts.

## Deployment

Everything is defined in `docker-compose.yaml` and runs locally. Services:

| Service              | Role                                                |
| -------------------- | --------------------------------------------------- |
| `postgres`           | pgvector — documents, chunks, embeddings, logs      |
| `app`                | Streamlit interface + monitoring dashboard          |
| `langfuse`           | Langfuse web (UI + ingestion API)                   |
| `langfuse-worker`    | Langfuse worker (processes traces into ClickHouse)  |
| `langfuse-clickhouse`| trace analytics store                               |
| `langfuse-minio`     | S3-compatible blob storage for traces               |
| `langfuse-minio-init`| one-shot: creates the MinIO bucket on first start   |
| `langfuse-redis`     | queue + cache                                       |
| `langfuse-db`        | Langfuse's own Postgres (separate from the app DB)  |

The app container installs the project's dependencies from the pinned
`uv.lock`, copies all runtime packages (`app`, `agents`, `retrieval`,
`monitoring`, `ingestion`), and pre-downloads the cross-encoder model at build
time so the first query does not stall on a model download.

### Tracing (Langfuse) — one-time setup

Langfuse self-hosts in the stack but starts with an empty database, so you
create an account and keys before traces can flow. This is one-time per fresh
volume; if you run `docker compose down -v`, the Langfuse database is wiped and
you repeat it.

1. Open http://localhost:3000 and sign up (any email/password; there is no mail
   server, so nothing is sent or verified). Save the password.
2. Create an organization and project (e.g. `PHEA` / `phea-dev`).
3. In project settings, create an API key pair.
4. Put the keys in `.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=http://localhost:3000
   ```
5. Restart the app: `docker compose up -d app`.

Langfuse v3 requires both the `langfuse` (web) and `langfuse-worker` containers;
both are in the compose file. The MinIO bucket is created automatically by the
`langfuse-minio-init` service. The app degrades gracefully without Langfuse:
queries still work and log to Postgres, only tracing is skipped. A 401 on trace
export means the keys are missing, wrong, or from a different project.

## Reproducibility

- **Pinned dependencies.** All Python dependencies are pinned in `uv.lock`; the
  app image installs with `uv sync --frozen`.
- **Pinned corpus.** Ingestion loads the frozen snapshot `data/pubmed.jsonl`, so
  the corpus is deterministic and matches the corpus the evaluation was built
  against. The snapshot was fetched from PubMed in the range 2018–2026; a live
  re-pull is available but drifts the corpus and requires re-running evaluation.
- **Deterministic evaluation inputs.** `evaluation/questions.yaml` (24
  questions) and `evaluation/qrels.jsonl` (431 graded judgments) are checked in,
  so retrieval and LLM evaluation reproduce without re-grading.
- **Full setup sequence** is in [Quick start](#quick-start); the one manual step
  that cannot be scripted is the Langfuse account/key creation, documented above.

## Roadmap

- **Weighted or BM25-backed hybrid** — the measured finding is that equal-weight
  RRF underperforms dense here because the two retrievers are too unequal. A
  stronger lexical ranker (BM25 via a Postgres extension) or dense-weighted
  fusion is the path to making hybrid competitive.
- **Scheduled ingestion** — the pinned/live pipeline design supports periodic
  re-pulls of every source via an orchestrator (e.g. Kestra/Airflow), paired
  with an evaluation re-run.

## Repository layout

```
app/            Streamlit interface (main.py) + pages/ (monitoring dashboard)
agents/         generation (generate.py) and the query router (router.py)
retrieval/      search.py (lexical, dense), fusion.py (RRF), rerank.py
ingestion/      pipeline.py (dlt), embed.py, schema.sql, indexes.sql
evaluation/     questions.yaml, qrels.jsonl, retrieval_metrics.py, judge.py,
                router_eval.py, build_qrels.py
monitoring/     store.py (query + feedback persistence and dashboard reads)
docker/         Dockerfile.app, Dockerfile.ingestion
data/           pubmed.jsonl, cms.jsonl (pinned corpus snapshots)
```

## Stack

Python 3.12, uv, Docker Compose, Postgres 16 + pgvector, dlt, OpenAI
(`text-embedding-3-small`, `gpt-4o`, `gpt-4o-mini`),
`BAAI/bge-reranker-v2-m3` (local), LangGraph, Streamlit, Langfuse v3
(self-hosted with ClickHouse, MinIO, Redis).