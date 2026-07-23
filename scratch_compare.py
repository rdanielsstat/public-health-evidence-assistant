# scratch_compare.py  (repo root, delete later)
import collections
from ingestion.fetch_pubmed import search, fetch_xml, parse_batch

MESH_ONLY = {
    "readmissions": '"Patient Readmission"[MeSH Terms]',
    "infections":   '"Cross Infection"[MeSH Terms]',
    "safety":       '"Patient Safety"[MeSH Terms]',
    "mortality":    '"Hospital Mortality"[MeSH Terms]',
    "quality":      '"Quality Indicators, Health Care"[MeSH Terms]',
}

UNION = {
    "readmissions": (
        '("Patient Readmission"[MeSH Terms] OR "patient readmission"[tiab] '
        'OR "hospital readmission*"[tiab] OR "30-day readmission*"[tiab])'
    ),
    "infections": (
        '("Cross Infection"[MeSH Terms] OR "healthcare-associated infection*"[tiab] '
        'OR "hospital-acquired infection*"[tiab] OR "nosocomial infection*"[tiab])'
    ),
    "safety": (
        '("Patient Safety"[MeSH Terms] OR "patient safety"[tiab] '
        'OR "adverse event*"[ti] OR "medical error*"[tiab])'
    ),
    "mortality": (
        '("Hospital Mortality"[MeSH Terms] OR "in-hospital mortality"[tiab] '
        'OR "inpatient mortality"[tiab] OR "hospital mortality"[tiab])'
    ),
    "quality": (
        '("Quality Indicators, Health Care"[MeSH Terms] '
        'OR "quality indicator*"[ti] OR "quality measure*"[ti] '
        'OR "performance measure*"[ti])'
    ),
}

N = 200

for topic in MESH_ONLY:
    a = set(search(MESH_ONLY[topic], N))
    b = set(search(UNION[topic], N))
    new = sorted(b - a)

    recs = parse_batch(fetch_xml(new)) if new else []
    years = collections.Counter(r["published_year"] for r in recs)
    recent = sum(v for k, v in years.items() if k and k >= 2024)
    no_mesh = sum(1 for r in recs if not r["mesh_terms"])

    print(f"\n{'=' * 70}")
    print(f"{topic}: {len(new)} new of {N}  |  {len(recs)} survived filters")
    print(f"  2024+: {recent}   no mesh: {no_mesh}")
    print(f"  years: {dict(sorted((k, v) for k, v in years.items() if k))}")
    print()
    for r in recs[:8]:
        print(f"  {r['published_year']} | {(r['title'] or '')[:80]}")