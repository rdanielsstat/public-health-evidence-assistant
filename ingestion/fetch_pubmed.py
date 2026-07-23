# ingestion/fetch_pubmed.py
"""Fetch and parse PubMed records via NCBI E-utilities.

Run standalone to write parsed records to data/pubmed.jsonl:
    uv run python -m ingestion.fetch_pubmed
"""

import json
import logging
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DATE_FILTER = '("2018"[PDAT] : "2026"[PDAT]) AND english[lang] AND hasabstract'
BATCH_SIZE = 200
REQUEST_DELAY = 0.4  # stay under 3 req/sec without an API key

TOPICS = {
    "readmissions": '"Patient Readmission"[MeSH Terms]',
    "infections": '"Cross Infection"[MeSH Terms]',
    "safety": '"Patient Safety"[MeSH Terms]',
    "mortality": '"Hospital Mortality"[MeSH Terms]',
    "quality": '"Quality Indicators, Health Care"[MeSH Terms]',
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
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


def search(term: str, retmax: int = 120) -> list[str]:
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


def parse_article(pa: ET.Element) -> dict | None:
    """Parse one <PubmedArticle> into a record dict. Returns None if unusable."""
    pmid_el = pa.find(".//MedlineCitation/PMID")
    if pmid_el is None or not pmid_el.text:
        return None
    pmid = pmid_el.text.strip()

    abstract = parse_abstract(pa)
    if not abstract:
        log.debug("pmid %s has no abstract, skipping", pmid)
        return None

    if len(abstract.split()) < 30:
        log.debug("pmid %s abstract too short (%d words), skipping",
                  pmid, len(abstract.split()))
        return None

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

    pub_types = [
        p.text.strip()
        for p in pa.findall(".//PublicationTypeList/PublicationType")
        if p.text
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
    }


def parse_batch(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    records = []
    for pa in root.findall(".//PubmedArticle"):
        try:
            rec = parse_article(pa)
        except Exception:
            log.exception("failed to parse an article, skipping")
            continue
        if rec:
            records.append(rec)
    return records


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def collect(per_topic: int = 120) -> list[dict]:
    """Search all topics, fetch records, merge topic labels across duplicates."""
    pmid_topics: dict[str, set[str]] = {}

    for topic, term in TOPICS.items():
        pmids = search(term, retmax=per_topic)
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