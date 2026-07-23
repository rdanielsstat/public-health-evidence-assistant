# ingestion/fetch_pubmed.py
"""Fetch and parse PubMed records via NCBI E-utilities.

Run standalone to write parsed records to data/pubmed.jsonl:
    uv run python -m ingestion.fetch_pubmed
"""

import json
import logging
import re
import time
import collections
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DATE_FILTER = '("2018"[PDAT] : "2026"[PDAT]) AND english[lang] AND hasabstract'
BATCH_SIZE = 200
REQUEST_DELAY = 0.4  # stay under 3 req/sec without an API key

DROP_REASONS: collections.Counter[str] = collections.Counter()

SUBSTANTIVE_TYPES = {
    "Journal Article",
    "Randomized Controlled Trial",
    "Clinical Trial",
    "Observational Study",
    "Systematic Review",
    "Meta-Analysis",
    "Multicenter Study",
    "Comparative Study",
    "Evaluation Study",
    "Validation Study",
    "Case Reports",
    "Review",
    "Practice Guideline",
    "Guideline",
}

EXCLUDED_TYPES = {
    "Editorial", "Comment", "News", "Newspaper Article",
    "Letter", "Biography", "Historical Article", "Interview",
    "Portrait", "Published Erratum",
}

TOPICS = {
    "readmissions": (
        '("Patient Readmission"[MeSH Terms] OR "patient readmission"[tiab] '
        'OR "hospital readmission*"[tiab] OR "30-day readmission*"[tiab])',
        130,
    ),
    "infections": (
        '("Cross Infection"[MeSH Terms] OR "healthcare-associated infection*"[tiab] '
        'OR "hospital-acquired infection*"[tiab] OR "nosocomial infection*"[tiab])',
        130,
    ),
    "safety": (
        '("Patient Safety"[MeSH Terms] OR "patient safety"[tiab] '
        'OR "adverse event*"[ti] OR "medical error*"[tiab])',
        160,
    ),
    "mortality": (
        '("Hospital Mortality"[MeSH Terms] OR "in-hospital mortality"[tiab] '
        'OR "inpatient mortality"[tiab] OR "hospital mortality"[tiab])',
        125,
    ),
    "quality": (
        '("Quality Indicators, Health Care"[MeSH Terms] OR "quality indicator*"[ti] '
        'OR "quality measure*"[ti] OR "performance measure*"[ti])',
        130,
    ),
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _get(path: str, params: dict) -> httpx.Response:
    r = httpx.get(f"{BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _post(path: str, data: dict) -> httpx.Response:
    r = httpx.post(f"{BASE}/{path}", data=data, timeout=90)
    r.raise_for_status()
    return r


def search(term: str, retmax: int) -> list[str]:
    """Return PMIDs for a MeSH term, relevance-ranked."""
    r = _get("esearch.fcgi", {
        "db": "pubmed",
        "term": f"{term} AND {DATE_FILTER}",
        "retmax": retmax,
        "sort": "relevance",
        "retmode": "json",
    })
    return r.json()["esearchresult"]["idlist"]


def fetch_xml(pmids: list[str]) -> str:
    """Fetch full records for up to BATCH_SIZE PMIDs. POST, since URLs get long."""
    r = _post("efetch.fcgi", {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    })
    return r.text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _text(el: ET.Element | None) -> str | None:
    """Flatten an element's text including any inline markup children."""
    if el is None:
        return None
    s = "".join(el.itertext()).strip()
    return s or None


def parse_year(article: ET.Element) -> int | None:
    """Extract publication year, tolerating PubMed's several date formats."""
    # Preferred: Journal/JournalIssue/PubDate
    pubdate = article.find(".//Journal/JournalIssue/PubDate")
    if pubdate is not None:
        year_el = pubdate.find("Year")
        if year_el is not None and year_el.text:
            try:
                return int(year_el.text[:4])
            except ValueError:
                pass
        # MedlineDate is free text: "2023 Jul-Aug 01", "2019 Winter", etc.
        medline = pubdate.find("MedlineDate")
        if medline is not None and medline.text:
            m = re.search(r"\b(19|20)\d{2}\b", medline.text)
            if m:
                return int(m.group(0))

    # Fallback: electronic ArticleDate
    art_date = article.find(".//ArticleDate/Year")
    if art_date is not None and art_date.text:
        try:
            return int(art_date.text[:4])
        except ValueError:
            pass

    return None


def parse_abstract(article: ET.Element) -> str | None:
    """Join abstract sections, preserving structured-abstract labels."""
    nodes = article.findall(".//Abstract/AbstractText")
    if not nodes:
        return None

    parts = []
    for node in nodes:
        text = _text(node)
        if not text:
            continue
        label = node.get("Label")
        parts.append(f"{label.strip()}: {text}" if label else text)

    joined = "\n\n".join(parts).strip()
    return joined or None


def parse_article(pa: ET.Element) -> tuple[dict | None, str | None]:
    """Parse one <PubmedArticle>. Returns (record, None) or (None, drop_reason)."""
    pmid_el = pa.find(".//MedlineCitation/PMID")
    if pmid_el is None or not pmid_el.text:
        return None, "no_pmid"
    pmid = pmid_el.text.strip()

    abstract = parse_abstract(pa)
    if not abstract:
        log.debug("pmid %s has no abstract, skipping", pmid)
        return None, "no_abstract"

    if len(abstract.split()) < 30:
        log.debug("pmid %s abstract too short (%d words), skipping",
                  pmid, len(abstract.split()))
        return None, "abstract_too_short"

    pub_types = [
        p.text.strip()
        for p in pa.findall(".//PublicationTypeList/PublicationType")
        if p.text
    ]

    if EXCLUDED_TYPES & set(pub_types) and not (SUBSTANTIVE_TYPES & set(pub_types)):
        log.debug("pmid %s excluded by publication type %s", pmid, pub_types)
        return None, "excluded_publication_type"

    title = _text(pa.find(".//Article/ArticleTitle"))
    journal = _text(pa.find(".//Journal/Title"))

    doi = None
    for aid in pa.findall(".//ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi" and aid.text:
            doi = aid.text.strip()
            break

    mesh = [
        d.text.strip()
        for d in pa.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
        if d.text
    ]

    citation = pa.find(".//MedlineCitation")
    indexing_method = citation.get("IndexingMethod") if citation is not None else None

    return {
        "doc_id": pmid,
        "source": "pubmed",
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "doi": doi,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "published_year": parse_year(pa),
        "mesh_terms": mesh,
        "publication_types": pub_types,
        "indexing_method": indexing_method,
    }, None


def parse_batch(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    records = []
    for pa in root.findall(".//PubmedArticle"):
        try:
            rec, reason = parse_article(pa)
        except Exception:
            DROP_REASONS["parse_error"] += 1
            log.exception("failed to parse an article, skipping")
            continue
        if rec:
            records.append(rec)
        else:
            DROP_REASONS[reason] += 1
    return records


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def collect() -> list[dict]:
    """Search all topics, fetch records, merge topic labels across duplicates."""
    DROP_REASONS.clear()
    pmid_topics: dict[str, set[str]] = {}

    for topic, (term, retmax) in TOPICS.items():
        pmids = search(term, retmax=retmax)
        log.info("%s: %d pmids", topic, len(pmids))
        for pmid in pmids:
            pmid_topics.setdefault(pmid, set()).add(topic)
        time.sleep(REQUEST_DELAY)

    all_pmids = sorted(pmid_topics)
    total_hits = sum(len(v) for v in pmid_topics.values())
    log.info(
        "%d unique pmids from %d hits (%d multi-topic)",
        len(all_pmids), total_hits, total_hits - len(all_pmids),
    )

    records = []
    for i in range(0, len(all_pmids), BATCH_SIZE):
        batch = all_pmids[i:i + BATCH_SIZE]
        log.info("fetching %d-%d", i, i + len(batch))
        records.extend(parse_batch(fetch_xml(batch)))
        time.sleep(REQUEST_DELAY)

    if DROP_REASONS:
        log.info("dropped %d records:", sum(DROP_REASONS.values()))
        for reason, n in DROP_REASONS.most_common():
            log.info("  %-28s %d", reason, n)

    for rec in records:
        rec["topics"] = sorted(pmid_topics[rec["doc_id"]])

    return records


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    records = collect()

    out = Path("data/pubmed.jsonl")
    out.parent.mkdir(exist_ok=True)
    with out.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    log.info("wrote %d records to %s", len(records), out)

    # quick sanity summary
    no_year = sum(1 for r in records if r["published_year"] is None)
    no_mesh = sum(1 for r in records if not r["mesh_terms"])
    no_title = sum(1 for r in records if not r["title"])
    multi = sum(1 for r in records if len(r["topics"]) > 1)
    lengths = sorted(len(r["abstract"].split()) for r in records)

    log.info("missing year: %d", no_year)
    log.info("missing mesh: %d", no_mesh)
    log.info("missing title: %d", no_title)
    log.info("multi-topic: %d", multi)
    log.info(
        "abstract words  min %d  p50 %d  p95 %d  max %d",
        lengths[0],
        lengths[len(lengths) // 2],
        lengths[int(len(lengths) * 0.95)],
        lengths[-1],
    )


if __name__ == "__main__":
    main()