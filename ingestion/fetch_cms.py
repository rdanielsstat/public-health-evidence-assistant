# ingestion/fetch_cms.py
"""Fetch CMS hospital program and measure descriptions from the Provider Data
Catalog (formerly Hospital Compare) as citable policy documents.

The catalog exposes one descriptive paragraph per dataset via a public,
unauthenticated DCAT metastore. That paragraph is the citable unit here: it
describes a CMS program or measure (for example the Hospital Readmissions
Reduction Program, the HAC Reduction Program, or the Healthcare-Associated
Infections measures) in prose, with a landing page that links to the official
source. We keep those descriptions and deliberately discard the per-hospital
score tables, which are data rather than citable evidence.

Descriptions are mapped to the same five topics as the PubMed corpus by keyword,
so policy and literature share a topic vocabulary and cross-source questions can
retrieve from both. Datasets that do not map to any of the five topics, and the
handful of general-information/administrative datasets, are dropped.

Run standalone to write the pinned snapshot:
    uv run python -m ingestion.fetch_cms
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

METASTORE = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items"
)
CMS_SNAPSHOT = Path("data/cms.jsonl")

# Only hospital-level program/measure datasets carry the policy descriptions we
# want. Other themes (dialysis, hospice, nursing homes, home health) are out of
# scope for an inpatient quality/safety corpus.
KEEP_THEME = "Hospitals"

# Keyword -> topic. Order matters only for reporting; a dataset can map to
# several topics (its description is one document carrying all matched topics),
# mirroring the multi-topic handling on the PubMed side.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "readmissions": (
        "readmission", "readmissions reduction", "unplanned hospital visit",
        "excess days in acute care",
    ),
    "infections": (
        "healthcare associated infection", "healthcare-associated infection",
        "surgical site infection", "central line", "catheter associated",
        "catheter-associated", "mrsa", "c.difficile", "c. difficile",
    ),
    "mortality": (
        "mortality", "death", "deaths", "30-day death",
    ),
    "safety": (
        "hospital-acquired condition", "patient safety", "adverse event",
        "psi", "complication", "complications", "safety",
    ),
    "quality": (
        "quality measure", "quality indicator", "quality of patient care",
        "timely and effective care", "value-based purchasing",
        "value based purchasing", "performance measure", "total performance",
    ),
}

# Datasets whose descriptions are administrative rather than substantive policy
# text, dropped even if they land in a kept theme.
DROP_TITLE_SUBSTRINGS = (
    "Footnote Crosswalk", "Measure Dates", "Data Updates",
    "General Information", "Zip", "Provider Level Data",
)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def _get_metastore() -> list[dict]:
    r = httpx.get(METASTORE, timeout=60, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _match_topics(title: str, description: str, keywords: list[str]) -> list[str]:
    """Return the sorted set of topics whose keywords appear in the dataset's
    title, description, or catalog keywords."""
    hay = " ".join([title, description, " ".join(keywords)]).lower()
    matched = {
        topic
        for topic, kws in TOPIC_KEYWORDS.items()
        if any(kw in hay for kw in kws)
    }
    return sorted(matched)


def _clean(text: str) -> str:
    """Collapse whitespace in a description paragraph."""
    return re.sub(r"\s+", " ", (text or "")).strip()


# Geographic aggregation suffixes. The same CMS program is published once per
# level (Hospital/Facility base, plus National and State rollups) with the same
# description bar one word. We keep one document per program, preferring the
# base level, and drop the rollups.
_GEO_SUFFIX = re.compile(
    r"\s*[-\u2013]\s*(Hospital|Facility|National|State|by State|by Facility)\s*$",
    re.IGNORECASE,
)


def _program_key(title: str) -> str:
    """Normalized program name with any geographic-level suffix removed, used to
    collapse Hospital/National/State variants of one program to a single doc."""
    return _GEO_SUFFIX.sub("", title).strip().lower()


def _geo_rank(title: str) -> int:
    """Preference when choosing which variant to keep: base level first, then
    National, then State. Lower is preferred."""
    tail = title.lower().rsplit("-", 1)[-1].strip()
    if tail in ("national",):
        return 1
    if tail in ("state", "by state"):
        return 2
    return 0  # Hospital / Facility / no suffix = base


def collect() -> list[dict]:
    """Fetch the catalog, keep hospital program/measure descriptions that map to
    our topics, de-duplicate identical descriptions, and return document records
    in the same shape the PubMed collector produces (plus source='cms',
    doc_type='policy')."""
    catalog = _get_metastore()
    log.info("metastore returned %d datasets", len(catalog))

    by_program: dict[str, dict] = {}  # program key -> best record so far
    dropped = {"theme": 0, "admin": 0, "no_topic": 0, "geo_variant": 0, "no_desc": 0}

    for ds in catalog:
        themes = ds.get("theme") or []
        if KEEP_THEME not in themes:
            dropped["theme"] += 1
            continue

        title = _clean(ds.get("title", ""))
        if any(sub.lower() in title.lower() for sub in DROP_TITLE_SUBSTRINGS):
            dropped["admin"] += 1
            continue

        description = _clean(ds.get("description", ""))
        if not description:
            dropped["no_desc"] += 1
            continue

        keywords = ds.get("keyword") or []
        topics = _match_topics(title, description, keywords)
        if not topics:
            dropped["no_topic"] += 1
            continue

        identifier = ds.get("identifier", "")
        landing = ds.get("landingPage") or (
            f"https://data.cms.gov/provider-data/dataset/{identifier}"
        )

        record = {
            "doc_id": f"cms-{identifier}",
            "source": "cms",
            "doc_type": "policy",
            "title": title,
            "abstract": description,
            "journal": "CMS Provider Data Catalog",
            "doi": None,
            "url": landing,
            "published_year": _issued_year(ds),
            "mesh_terms": [],
            "publication_types": ["CMS Program"] if "Reduction Program" in title
                                 or "Value-Based" in title else ["CMS Measure"],
            "indexing_method": None,
            "topics": topics,
        }

        # Collapse Hospital/National/State variants of one program to a single
        # document, keeping the base (Hospital/Facility) level when present.
        key = _program_key(title)
        existing = by_program.get(key)
        if existing is None:
            by_program[key] = record
        else:
            dropped["geo_variant"] += 1
            if _geo_rank(title) < _geo_rank(existing["title"]):
                by_program[key] = record

    records = list(by_program.values())
    log.info(
        "kept %d CMS policy documents; dropped %s",
        len(records), dropped,
    )
    return records


def _issued_year(ds: dict) -> int | None:
    issued = ds.get("issued") or ds.get("modified") or ""
    m = re.match(r"(\d{4})", issued)
    return int(m.group(1)) if m else None


def write_snapshot(path: Path = CMS_SNAPSHOT) -> int:
    records = collect()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    log.info("wrote %d records to %s", len(records), path)
    return len(records)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    n = write_snapshot()
    print(f"wrote {n} CMS policy documents to {CMS_SNAPSHOT}")


if __name__ == "__main__":
    main()
