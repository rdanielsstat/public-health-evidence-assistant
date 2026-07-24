# Public Health Evidence Assistant

A retrieval-augmented generation (RAG) and agent system that answers questions
about inpatient healthcare quality and safety using peer-reviewed evidence and
federal policy documents.

## Problem

Clinicians, hospital quality teams, and health policy analysts need to answer
questions that span two bodies of knowledge that live in different places and
speak different languages:

- **What does the evidence say?** Peer-reviewed studies on which interventions
  reduce readmissions, infections, and inpatient mortality.
- **What does policy require?** Federal programs such as the CMS Hospital
  Readmissions Reduction Program that tie reimbursement to these same outcomes.

Answering a question like *"what interventions reduce 30-day readmissions, and
how do they align with CMS penalty criteria?"* currently means searching
PubMed, then separately searching CMS and AHRQ documentation, then reconciling
the two by hand.

This project builds a single interface over both, with citations back to source
documents so answers can be verified.

### Example questions

- What interventions reduce 30-day readmissions for heart failure patients?
- Which infection prevention practices have the strongest evidence in ICU
  settings?
- How do CMS readmission penalty criteria compare to what the literature
  identifies as modifiable risk factors?
- What are the strongest predictors of inpatient mortality after sepsis?
- Has evidence on hand hygiene compliance interventions changed since 2020?

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

**Policy and guidance documents** — federal guidance covering the same
outcomes, including CDC healthcare-associated infection guidance and CMS
quality program documentation.

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
questions.

> **TODO:** replace this rationale with measured results once the evaluation
> set exists — title-prepended vs. abstract-only, compared on Recall@10.

The chunking layer is retained for policy and guidance documents (CDC, CMS),
which are substantially longer and do require splitting.

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

## Evaluation

markdown
### Relevance judgments

Retrieval candidates were pooled from both methods (top 10 each, union) and
graded 0/1/2 by `gpt-4o-mini`. This produced 431 judgments across 24
questions: 170 not relevant, 183 partially relevant, 78 highly relevant.

Grader calibration was checked against manual judgment on a stratified sample
of 36 pairs, 12 from each grade:

| Measure                              | Result      |
| ------------------------------------ | ----------- |
| Exact agreement                      | 23/36 (64%) |
| Within one grade                     | 34/36 (94%) |
| Manual grade stricter than model     | 13          |
| Manual grade more lenient than model | 0           |

Agreement is complete on grade 0 (12/12) and degrades as relevance increases:
5 of 12 grade-1 judgments and 8 of 12 grade-2 judgments would have been graded
lower manually. The disagreement is entirely one-directional, indicating the
automated grader is systematically more generous than a human judge rather
than noisy.

Two caveats bound this. Manual grading used titles only while the grader saw
full abstracts, so some strictness reflects insufficient information rather
than genuine disagreement. And the sample is stratified rather than
representative, so the 64% figure describes agreement within each grade band,
not across the actual 170/183/78 distribution.

The practical consequence is that absolute recall and NDCG values are
optimistic. Relative comparisons between retrieval methods are unaffected,
since the same judgments are applied to every variant.

### Pipeline variants

Variants are scored on an identical question set:

| Variant         | Retrieval                                   |
| --------------- | ------------------------------------------- |
| `no_retrieval`  | none — LLM answers from parametric knowledge |
| `lexical_only`  | Postgres full-text, `ts_rank_cd`            |
| `dense_only`    | pgvector cosine, `text-embedding-3-small`   |
| `hybrid_rrf`    | reciprocal rank fusion over both            |
| `hybrid_rerank` | RRF candidates reranked by cross-encoder    |

### The no-retrieval baseline

Most of this corpus predates current model training cutoffs, so a language
model can produce a fluent answer to most of these questions without any
retrieval at all. The baseline tests whether the retrieval pipeline adds value
rather than assuming it does.

On an initial spot check, the ungrounded baseline produced a *more
comprehensive* answer than the grounded pipeline — seven intervention
categories against four — and every clinical claim it made was broadly
accurate. Its citations were another matter. All seven PMIDs it supplied were
checked against PubMed:

| PMID     | Cited as                                      | Actually                                          |
| -------- | --------------------------------------------- | ------------------------------------------------- |
| 19414673 | Structured education programs, Jaarsma et al. | Phase II immunotoxin trial in hairy cell leukemia  |
| 19139356 | Transitional care review, Jack et al.         | Does not resolve                                   |
| 18467729 | Home health interventions, McAlister et al.   | Doxorubicin in pediatric hepatoblastoma            |
| 18467729 | Multidisciplinary care, Coleman et al.        | Same paper, cited twice under two attributions     |
| 26700000 | Medication reconciliation, Weir et al.        | Rac1 signalling in rat inflammatory pain           |
| 24685312 | Structured follow-up care, Hesselink et al.   | Gaucher disease cohort in South Florida            |
| 28167973 | Telehealth meta-analysis, Kitsiou et al.      | Caffeic acid and head/neck carcinoma cells         |

Zero of seven were correct. Six were valid PubMed identifiers pointing at
unrelated papers — a more dangerous failure than an invented number, since the
citation looks checkable and resolves to a real record. One identifier does not
exist at all. One paper was cited twice under two different author
attributions.

Answer quality and citation validity are therefore scored as separate
dimensions. A single quality score would rank the ungrounded baseline higher on
this question despite its sourcing being entirely fabricated.

Citation validity is checked mechanically rather than by an LLM judge: PMIDs
are extracted from the answer and tested for membership in the `documents`
table. Only groundedness and completeness require a judge.

## Status

Under development.
