# scratch_fetch.py  (repo root, delete later)
import json, time, httpx

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DATE_FILTER = '("2018"[PDAT] : "2026"[PDAT]) AND english[lang] AND hasabstract'

TOPICS = {
    "readmissions": '"Patient Readmission"[MeSH Terms]',
    "infections":   '"Cross Infection"[MeSH Terms]',
    "safety":       '"Patient Safety"[MeSH Terms]',
    "mortality":    '"Hospital Mortality"[MeSH Terms]',
    "quality":      '"Quality Indicators, Health Care"[MeSH Terms]',
}

def search(term, retmax=120):
    r = httpx.get(f"{BASE}/esearch.fcgi", params={
        "db": "pubmed",
        "term": f"{term} AND {DATE_FILTER}",
        "retmax": retmax,
        "sort": "relevance",
        "retmode": "json",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["esearchresult"]["idlist"]

def fetch(pmids):
    r = httpx.post(f"{BASE}/efetch.fcgi", data={
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }, timeout=60)
    r.raise_for_status()
    return r.text

if __name__ == "__main__":
    results = {}
    for topic, term in TOPICS.items():
        pmids = search(term)
        print(f"{topic:14s} {len(pmids)} pmids")
        results[topic] = pmids
        time.sleep(0.5)

    with open("scratch_pmids.json", "w") as f:
        json.dump(results, f, indent=2)

    all_pmids = [p for v in results.values() for p in v]
    unique = set(all_pmids)
    print(f"\ntotal {len(all_pmids)}, unique {len(unique)}, "
          f"collisions {len(all_pmids) - len(unique)}")

    sample = fetch(results["readmissions"][:5])
    with open("scratch_sample.xml", "w") as f:
        f.write(sample)
    print("wrote scratch_sample.xml")