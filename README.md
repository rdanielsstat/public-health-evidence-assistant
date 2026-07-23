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

## Scope

The corpus is deliberately narrow: inpatient healthcare quality and safety. 
This keeps the knowledge base coherent enough that retrieval quality can be 
measured meaningfully, while covering enough overlapping subtopics that 
questions genuinely require synthesis across documents.

### Knowledge base

**Peer-reviewed literature (PubMed)** — abstracts retrieved via the NCBI 
E-utilities API, indexed under five MeSH terms:

| Topic                             | MeSH term                         |
|-----------------------------------|-----------------------------------|
| Hospital readmissions             | `Patient Readmission`             |
| Healthcare-associated infections  | `Cross Infection`                 |
| Patient safety                    | `Patient Safety`                  |
| Hospital mortality                | `Hospital Mortality`              |
| Inpatient quality                 | `Quality Indicators, Health Care` |

Filtered to English-language records with abstracts, published 2018 or later.

**Policy and guidance documents** — federal guidance covering the same 
outcomes, including CDC healthcare-associated infection guidance and CMS 
quality program documentation.

These five topics overlap by design. Readmissions, infections, and mortality 
are all quality indicators, and a substantive question usually touches more 
than one. This overlap is what makes retrieval strategy matter: naive keyword 
search retrieves documents that share vocabulary rather than documents that 
answer the question.

### Scope

### Corpus characteristics

The five MeSH topics overlap conceptually but rarely collide at the
document level. Of 600 relevance-ranked search hits, 596 were unique
PMIDs — only 4 papers were returned under more than one topic:

| Topics                  | Title                                                          |
|-------------------------|----------------------------------------------------------------|
| quality, readmissions   | Moving Toward Paying for Outcomes in Medicaid                  |
| quality, safety         | Quality, Safety, and Value in Pediatric Spine Surgery          |
| mortality, readmissions | Transition of Care at Discharge from the ICU: A Scoping Review |
| mortality, quality      | Failure to Rescue: A Quality Indicator for Postoperative Care  |

This low collision rate is a property of relevance ranking rather than
of the corpus: the most on-topic papers for each MeSH term sit in
distinct neighborhoods. Cross-topic *questions* remain answerable, since
answering "which discharge interventions reduce both readmissions and
post-discharge mortality" requires retrieving from documents that
arrived under different topics, not documents indexed under both.
Ingestion merges topic labels for the papers that do collide, so each
document is stored once with all applicable labels.

### Chunking

Chunk size was determined empirically rather than by default. Across
the ingested corpus, PubMed abstracts are short and tightly bounded:

| Percentile | Words |
|------------|-------|
| min        | 30    |
| median     | 108   |
| 95th       | 201   |
| max        | 268   |

Every abstract fits within a single embedding without truncation, so
PubMed documents are indexed whole — title prepended to abstract, one
chunk per document. Splitting a 108-word abstract would fragment the
context that makes it retrievable, and the standard 500-token chunking
default would be a no-op on this corpus anyway.

Titles are prepended to abstract text before embedding. They may carry
dense, query-like phrasing ("Implementing a Discharge Follow-up Phone
Call Program Reduces Readmission Rates"), and discarding them measurably
weakens retrieval for keyword-style queries.

Records with abstracts under 30 words are excluded during ingestion.
These are not abstracts but placeholder stubs ("According to this
study.") and editorial teasers, which would embed to noise and pollute
retrieval results.

The chunking layer is retained for policy and guidance documents (CDC,
CMS), which are substantially longer and do require splitting.

### Example questions

- What interventions reduce 30-day readmissions for heart failure patients?
- Which infection prevention practices have the strongest evidence in ICU 
  settings?
- How do CMS readmission penalty criteria compare to what the literature 
  identifies as modifiable risk factors?
- What are the strongest predictors of inpatient mortality after sepsis?
- Has evidence on hand hygiene compliance interventions changed since 2020?

## Status

Under development. See the evaluation criteria sections below for current 
progress.