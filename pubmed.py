#!/usr/bin/env python3
"""
pubmed.py - Fetch ALL available PubMed information for a drug, in parallel.

Usage (import):
    from pubmed import pubmed
    result = pubmed("Semaglutide")            # result["articles"], result["files"], ...

Usage (command line):
    python pubmed.py Semaglutide
    python pubmed.py Semaglutide --workers 8 --api-key YOUR_NCBI_KEY
    python pubmed.py "tirzepatide" --clinical-only --formats csv json
    python pubmed.py Semaglutide --no-synonyms --extra-terms Ozempic Wegovy Rybelsus
    python pubmed.py Semaglutide --raw-synonyms     # every PubChem synonym, unfiltered
    python pubmed.py Semaglutide --keep-all         # also keep rows with no trial registry ID

How it works
    1. (optional) Expand the drug name into synonyms via PubChem.
    2. esearch  -> all matching PMIDs. Searches larger than PubMed's 10,000-result
       cap are automatically split into date ranges.
    3. efetch   -> full article XML, in batches, across a thread pool
       (global rate limiter keeps you inside NCBI's limits).
    4. Parse every available field and write CSV / JSON / XLSX.

Rate limits (NCBI): 3 requests/sec without an API key, 10/sec with one.
Get a free key at https://www.ncbi.nlm.nih.gov/account/settings/

Requires: requests   (optional: openpyxl for .xlsx output)
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests

try:
    from fetch_search_terms import fetch_search_terms as _fetch_search_terms
except ImportError:
    _fetch_search_terms = None

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBCHEM_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{}/synonyms/JSON"

ESEARCH_CAP = 9999      # PubMed esearch cannot page past 10,000 results per query
FETCH_BATCH = 200       # PMIDs per efetch request
MAX_RETRIES = 5
TOOL_NAME = "pubmed_drug_fetcher"

CLINICAL_FILTER = (
    '("clinical trial"[Publication Type] OR "randomized controlled trial"[Publication Type] '
    'OR "meta-analysis"[Publication Type] OR "systematic review"[Publication Type] '
    'OR "observational study"[Publication Type])'
)

PHASE_MAP = [("Phase IV", "Phase 4"), ("Phase III", "Phase 3"),
             ("Phase II", "Phase 2"), ("Phase I", "Phase 1")]
MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Trial registry patterns for abstract text (fallback when DataBankList is absent)
REGISTRY_PATTERNS = [
    (r"NCT\d{8}", "ClinicalTrials.gov"),
    (r"EUCTR\d{4}-\d{6}-\d{2}(?:-[A-Z]{2})?", "EU Clinical Trials Register"),
    (r"\d{4}-\d{6}-\d{2}(?=\D|$)", "EudraCT"),
    (r"CTRI/\d{4}/\d{2,3}/\d{6}", "CTRI (India)"),
    (r"ISRCTN\d{8}", "ISRCTN"),
    (r"ACTRN\d{14}", "ANZCTR"),
    (r"ChiCTR-?[A-Za-z]{0,5}-?\d{8,10}", "ChiCTR"),
    (r"JPRN-[A-Za-z0-9\-]+", "JPRN"),
    (r"UMIN\d{9}", "UMIN-CTR"),
    (r"jRCT\w\d{9}", "jRCT"),
    (r"KCT\d{7}", "KCT (Korea)"),
    (r"DRKS\d{8}", "DRKS (Germany)"),
    (r"NTR\d{3,4}", "Netherlands Trial Register"),
    (r"PACTR\d{15}", "PACTR (Africa)"),
    (r"TCTR\d{11}", "TCTR (Thailand)"),
    (r"IRCT\d{14,20}N\d{1,3}", "IRCT (Iran)"),
    (r"RBR-[a-z0-9]{6,8}", "ReBec (Brazil)"),
]
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_EXCLUDED_REGISTRIES: frozenset[str] = frozenset({
    "bioproject", "geo", "figshare", "isrctn", "pdb", "umin-ctr",
})


# ──────────────────────────────────────────────────────────────────────────
# Networking: global rate limiter + retrying HTTP helper
# ──────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """Thread-safe limiter: guarantees >= `interval` seconds between request starts."""

    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / rate_per_sec
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class NCBIClient:
    def __init__(self, api_key=None, email=None):
        self.api_key = api_key
        self.email = email
        # Stay slightly under NCBI's published limits
        self.limiter = RateLimiter(9.0 if api_key else 2.7)
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            s = requests.Session()
            s.headers["User-Agent"] = f"{TOOL_NAME}/1.0"
            self._local.s = s
        return self._local.s

    def _base_params(self) -> dict:
        p = {"tool": TOOL_NAME}
        if self.api_key:
            p["api_key"] = self.api_key
        if self.email:
            p["email"] = self.email
        return p

    def request(self, method, url, params=None, data=None, timeout=60) -> requests.Response:
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.limiter.wait()
            try:
                if method == "GET":
                    r = self._session().get(url, params=params, timeout=timeout)
                else:
                    r = self._session().post(url, data=data, timeout=timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                return r
            except (requests.RequestException, ET.ParseError) as e:
                last_err = e
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"Request failed after {MAX_RETRIES} attempts: {last_err}")

    def esearch(self, term, mindate=None, maxdate=None, retmax=0) -> dict:
        params = self._base_params()
        params.update({"db": "pubmed", "term": term, "retmode": "json",
                       "retmax": retmax, "sort": "pub_date"})
        if mindate and maxdate:
            params.update({"datetype": "pdat", "mindate": mindate, "maxdate": maxdate})
        r = self.request("POST", ESEARCH_URL, data=params, timeout=60)
        return r.json()["esearchresult"]

    def efetch(self, pmids) -> bytes:
        data = self._base_params()
        data.update({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
        return self.request("POST", EFETCH_URL, data=data, timeout=120).content


# ──────────────────────────────────────────────────────────────────────────
# Step 0: synonym expansion (PubChem)
# ──────────────────────────────────────────────────────────────────────────
_NOISE = re.compile(
    r"^(SCHEMBL|CHEMBL|DTXSID|DTXCID|UNII|HSDB|AKOS|BCP|CS-|HY-|MFCD|BDBM|NSC|EINECS|"
    r"CAS-|ZINC|AC-|AS-|SR-|BRN|CCRIS|EC )", re.I)


def get_synonyms(drug: str, limit: int | None = None, raw: bool = False) -> list[str]:
    """
    Pull ALL synonyms from PubChem (brand names, dev codes, etc.). Best-effort.
    limit=None -> no cap.  raw=True -> skip the junk filter and return every PubChem synonym.
    """
    try:
        r = requests.get(PUBCHEM_URL.format(quote(drug)), timeout=20)
        if r.status_code != 200:
            return []
        syns = r.json()["InformationList"]["Information"][0]["Synonym"]
    except Exception as e:
        print(f"  [PubChem] synonym lookup skipped ({type(e).__name__})")
        return []

    out, seen = [], {drug.lower()}
    for s in syns:
        s = s.strip()
        low = s.lower()
        if not s or low in seen:
            continue
        if not raw and (not (3 <= len(s) <= 40) or _NOISE.match(s)
                        or re.fullmatch(r"[\d\-\s]+", s)          # CAS numbers etc.
                        or re.search(r"[\[\]{}(),;:/]", s)         # IUPAC-style fragments
                        or not re.search(r"[A-Za-z]", s)):
            continue
        seen.add(low)
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


# ──────────────────────────────────────────────────────────────────────────
# Step 1: search (auto-splits by date when > 10,000 hits)
# ──────────────────────────────────────────────────────────────────────────
def build_query(terms: list[str], clinical_only: bool) -> str:
    parts = []
    for t in terms:
        t = t.replace('"', "")
        parts.append(f'"{t}"[Title/Abstract]')
        parts.append(f'"{t}"[Supplementary Concept]')
    q = "(" + " OR ".join(parts) + ")"
    if clinical_only:
        q += " AND " + CLINICAL_FILTER
    return q


TERMS_PER_QUERY = 40   # synonyms per esearch query (keeps each query a sane size)


def build_queries(terms: list[str], clinical_only: bool) -> list[str]:
    return [build_query(terms[i:i + TERMS_PER_QUERY], clinical_only)
            for i in range(0, len(terms), TERMS_PER_QUERY)]


def collect_pmids(client: NCBIClient, query: str, y0: int, y1: int, m0=1, m1=12) -> list[str]:
    """Recursively split the date range until each slice fits under the esearch cap."""
    mindate = f"{y0}/{m0:02d}/01"
    maxdate = f"{y1}/{m1:02d}/31"
    res = client.esearch(query, mindate, maxdate, retmax=0)
    count = int(res.get("count", 0))
    if count == 0:
        return []
    if count <= ESEARCH_CAP:
        res = client.esearch(query, mindate, maxdate, retmax=count)
        return res.get("idlist", [])

    # Too many results -> bisect
    if y0 < y1:
        mid = (y0 + y1) // 2
        return (collect_pmids(client, query, y0, mid) +
                collect_pmids(client, query, mid + 1, y1))
    if m0 < m1:
        mid = (m0 + m1) // 2
        return (collect_pmids(client, query, y0, y0, m0, mid) +
                collect_pmids(client, query, y0, y0, mid + 1, m1))
    print(f"  [warn] {y0}-{m0:02d} still has {count} hits; taking first {ESEARCH_CAP}")
    return client.esearch(query, mindate, maxdate, retmax=ESEARCH_CAP).get("idlist", [])


def search_one(client: NCBIClient, query: str) -> list[str]:
    total = int(client.esearch(query, retmax=0).get("count", 0))
    if total == 0:
        return []
    if total <= ESEARCH_CAP:
        return client.esearch(query, retmax=total).get("idlist", [])
    return collect_pmids(client, query, 1800, datetime.now().year + 1)


def search_all(client: NCBIClient, queries: list[str], workers: int) -> list[str]:
    """Run every chunked query in parallel and union the PMIDs."""
    ids: list[str] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(queries))) as pool:
        futs = {pool.submit(search_one, client, q): i for i, q in enumerate(queries, 1)}
        for n, fut in enumerate(as_completed(futs), 1):
            got = fut.result()
            ids.extend(got)
            print(f"  query chunk {futs[fut]}/{len(queries)} done ({n}/{len(queries)}): {len(got):,} hits")
    return list(dict.fromkeys(ids))  # de-dupe, keep order


# ──────────────────────────────────────────────────────────────────────────
# Step 2: parsing
# ──────────────────────────────────────────────────────────────────────────
def txt(elem) -> str:
    """All text inside an element, including inline sub-tags (<i>, <sub>...)."""
    return "".join(elem.itertext()).strip() if elem is not None else ""


def ymd(elem) -> str:
    if elem is None:
        return ""
    y, m, d = (elem.findtext(k) or "" for k in ("Year", "Month", "Day"))
    m = MONTHS.get(m, m)
    return "-".join(p.zfill(2) if i else p for i, p in enumerate([y, m, d]) if p) if y else ""


def pub_date(art, journal) -> tuple[str, str]:
    """(best date string, year). Prefers electronic ArticleDate, falls back to print PubDate."""
    d = ymd(art.find("ArticleDate"))
    pd_elem = journal.find("JournalIssue/PubDate") if journal is not None else None
    print_date = ymd(pd_elem) if pd_elem is not None else ""
    if not print_date and pd_elem is not None:
        print_date = (pd_elem.findtext("MedlineDate") or "").strip()
    best = d or print_date
    m = re.search(r"(1[5-9]\d{2}|20\d{2})", best)
    return best, (m.group(1) if m else "")


def parse_article(pa: ET.Element) -> dict | None:
    mc = pa.find("MedlineCitation")
    art = mc.find("Article") if mc is not None else None
    if art is None:
        return None
    pd_ = pa.find("PubmedData")
    journal = art.find("Journal")

    pmid = (mc.findtext("PMID") or "").strip()

    # Abstract (structured labels preserved)
    abs_parts = []
    for ab in art.findall("Abstract/AbstractText"):
        label, body = ab.get("Label"), txt(ab)
        if body:
            abs_parts.append(f"{label}: {body}" if label else body)
    for ab in mc.findall("OtherAbstract/AbstractText"):      # non-English abstracts
        if txt(ab):
            abs_parts.append(f"[Other abstract] {txt(ab)}")
    abstract = " ".join(abs_parts)

    # Authors (all) + affiliations + ORCID
    authors, affils = [], []
    for a in art.findall("AuthorList/Author"):
        name = (a.findtext("CollectiveName") or
                f"{a.findtext('LastName') or ''} {a.findtext('ForeName') or a.findtext('Initials') or ''}".strip())
        if not name:
            continue
        a_aff = [txt(x) for x in a.findall("AffiliationInfo/Affiliation") if txt(x)]
        orcid = ""
        for ident in a.findall("Identifier"):
            if (ident.get("Source") or "").upper() == "ORCID":
                orcid = txt(ident)
        authors.append({"name": name, "affiliations": a_aff, "orcid": orcid})
        affils.extend(a_aff)
    affils = list(dict.fromkeys(affils))

    # Article IDs (doi, pmc, pii...)
    ids = {}
    for aid in pa.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") and aid.text:
            ids.setdefault(aid.get("IdType"), aid.text.strip())
    if "doi" not in ids:
        for eid in art.findall("ELocationID"):
            if eid.get("EIdType") == "doi" and eid.text:
                ids["doi"] = eid.text.strip()

    # Publication types / phase
    pub_types = [txt(p) for p in art.findall("PublicationTypeList/PublicationType") if txt(p)]
    phase = next((lab for key, lab in PHASE_MAP if any(key in pt for pt in pub_types)), "")

    # MeSH (descriptor/qualifier, major-topic flagged with *)
    mesh = []
    for mh in mc.findall("MeshHeadingList/MeshHeading"):
        d = mh.find("DescriptorName")
        if d is None:
            continue
        label = txt(d) + ("*" if d.get("MajorTopicYN") == "Y" else "")
        quals = [txt(q) + ("*" if q.get("MajorTopicYN") == "Y" else "")
                 for q in mh.findall("QualifierName")]
        mesh.append(f"{label}/{'/'.join(quals)}" if quals else label)

    keywords = [txt(k) for k in mc.findall("KeywordList/Keyword") if txt(k)]

    chemicals = [{"name": txt(c.find("NameOfSubstance")), "registry_number": c.findtext("RegistryNumber") or ""}
                 for c in mc.findall("ChemicalList/Chemical")]
    supp_mesh = [txt(s) for s in mc.findall("SupplMeshList/SupplMeshName") if txt(s)]

    grants = [{"id": g.findtext("GrantID") or "", "agency": g.findtext("Agency") or "",
               "country": g.findtext("Country") or ""} for g in art.findall("GrantList/Grant")]

    # Trial registry IDs: structured DataBankList first, regex fallback on the abstract
    registry_ids = {}
    for db in art.findall("DataBankList/DataBank"):
        name = (db.findtext("DataBankName") or "").strip()
        for acc in db.findall("AccessionNumberList/AccessionNumber"):
            if acc.text:
                registry_ids[acc.text.strip().upper()] = name
    for pat, reg in REGISTRY_PATTERNS:
        for m in re.findall(pat, f"{txt(art.find('ArticleTitle'))} {abstract}", re.I):
            registry_ids.setdefault(m.upper(), reg)

    # Corrections / retractions / commentary
    corrections = [f"{c.get('RefType')}: PMID {c.findtext('PMID') or ''}".strip()
                   for c in mc.findall("CommentsCorrectionsList/CommentsCorrections")]
    retracted = ("Retracted Publication" in pub_types or
                 any(c.startswith("RetractionIn") for c in corrections))

    n_refs = len(pa.findall("PubmedData/ReferenceList//Reference"))

    # Dates
    hist = {}
    for pdt in pa.findall("PubmedData/History/PubMedPubDate"):
        hist[pdt.get("PubStatus", "")] = ymd(pdt)

    date_str, year = pub_date(art, journal)
    pagination = art.findtext("Pagination/MedlinePgn") or art.findtext("Pagination/StartPage") or ""

    return {
        "pmid": pmid,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "title": txt(art.find("ArticleTitle")),
        "vernacular_title": txt(art.find("VernacularTitle")),
        "abstract": abstract,
        "publication_date": date_str,
        "year": year,
        "journal": (journal.findtext("Title") if journal is not None else "") or "",
        "journal_abbrev": (journal.findtext("ISOAbbreviation") if journal is not None else "") or "",
        "issn": (journal.findtext("ISSN") if journal is not None else "") or "",
        "volume": (journal.findtext("JournalIssue/Volume") if journal is not None else "") or "",
        "issue": (journal.findtext("JournalIssue/Issue") if journal is not None else "") or "",
        "pages": pagination,
        "language": "; ".join(l.text for l in art.findall("Language") if l.text),
        "publication_types": pub_types,
        "trial_phase": phase,
        "authors": authors,
        "n_authors": len(authors),
        "first_author": authors[0]["name"] if authors else "",
        "last_author": authors[-1]["name"] if authors else "",
        "affiliations": affils,
        "doi": ids.get("doi", ""),
        "pmcid": ids.get("pmc", ""),
        "pii": ids.get("pii", ""),
        "mesh_terms": mesh,
        "keywords": keywords,
        "chemicals": chemicals,
        "supplementary_mesh": supp_mesh,
        "grants": grants,
        "trial_registry_ids": registry_ids,
        "corrections_comments": corrections,
        "is_retracted": retracted,
        "n_references": n_refs,
        "publication_status": (pd_.findtext("PublicationStatus") if pd_ is not None else "") or "",
        "owner_status": mc.get("Status", ""),
        "date_completed": ymd(mc.find("DateCompleted")),
        "date_revised": ymd(mc.find("DateRevised")),
        "history_dates": hist,
        "country": mc.findtext("MedlineJournalInfo/Country") or "",
        "nlm_unique_id": mc.findtext("MedlineJournalInfo/NlmUniqueID") or "",
        "coi_statement": txt(mc.find("CoiStatement")),
        "copyright": txt(art.find("Abstract/CopyrightInformation")),
    }


# ──────────────────────────────────────────────────────────────────────────
# Step 3: parallel fetch
# ──────────────────────────────────────────────────────────────────────────
def fetch_batch(client: NCBIClient, batch: list[str]) -> list[dict]:
    root = ET.fromstring(client.efetch(batch))
    rows = [parse_article(pa) for pa in root.findall("PubmedArticle")]
    return [r for r in rows if r]


def fetch_all(client: NCBIClient, pmids: list[str], workers: int):
    batches = [pmids[i:i + FETCH_BATCH] for i in range(0, len(pmids), FETCH_BATCH)]
    rows, failed = [], []
    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_batch, client, b): b for b in batches}
        for fut in as_completed(futures):
            try:
                rows.extend(fut.result())
            except Exception as e:
                failed.extend(futures[fut])
                print(f"\n  [error] batch of {len(futures[fut])} failed: {e}")
            done += 1
            el = time.time() - t0
            eta = el / done * (len(batches) - done)
            print(f"\r  Fetched {done}/{len(batches)} batches | {len(rows):,} articles | "
                  f"{el:0.0f}s elapsed, ~{eta:0.0f}s left   ", end="", flush=True)
    print()
    return rows, failed


# ──────────────────────────────────────────────────────────────────────────
# Step 4: output
# ──────────────────────────────────────────────────────────────────────────
def flatten(row: dict) -> dict:
    """Make a nested record CSV/Excel friendly."""
    f = dict(row)
    f["publication_types"] = "; ".join(row["publication_types"])
    f["authors"] = "; ".join(a["name"] for a in row["authors"])
    f["author_orcids"] = "; ".join(f"{a['name']}={a['orcid']}" for a in row["authors"] if a["orcid"])
    f["affiliations"] = " | ".join(row["affiliations"])
    f["mesh_terms"] = "; ".join(row["mesh_terms"])
    f["keywords"] = "; ".join(row["keywords"])
    f["chemicals"] = "; ".join(f"{c['name']} ({c['registry_number']})" if c["registry_number"] else c["name"]
                               for c in row["chemicals"])
    f["supplementary_mesh"] = "; ".join(row["supplementary_mesh"])
    f["grants"] = "; ".join(" ".join(filter(None, [g["agency"], g["id"], g["country"]])) for g in row["grants"])
    f["trial_registry_ids"] = "; ".join(f"{k} ({v})" for k, v in row["trial_registry_ids"].items())
    f["corrections_comments"] = "; ".join(row["corrections_comments"])
    f["history_dates"] = "; ".join(f"{k}={v}" for k, v in row["history_dates"].items())
    return f


def write_outputs(rows, outdir, stem, formats, meta):
    os.makedirs(outdir, exist_ok=True)
    paths = []
    flat = [flatten(r) for r in rows]
    cols = list(flat[0].keys()) if flat else []

    if "csv" in formats and flat:
        p = os.path.join(outdir, f"{stem}.csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel reads UTF-8
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(flat)
        paths.append(p)

    if "json" in formats:
        p = os.path.join(outdir, f"{stem}.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"metadata": meta, "articles": rows}, fh, ensure_ascii=False, indent=2)
        paths.append(p)

    if "xlsx" in formats and flat:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            print("  [skip] xlsx: pip install openpyxl")
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Articles"
            ws.append(cols)
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", start_color="1F4E79")
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            for r in flat:
                ws.append([CONTROL_CHARS.sub("", str(r[c]))[:32000] for c in cols])
            wide = {"title": 60, "abstract": 90, "authors": 40, "affiliations": 50, "mesh_terms": 50}
            for i, c in enumerate(cols, 1):
                ws.column_dimensions[get_column_letter(i)].width = wide.get(c, 18)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

            s = wb.create_sheet("Summary")
            for k, v in meta.items():
                s.append([k, str(v)])
            s.column_dimensions["A"].width = 28
            s.column_dimensions["B"].width = 100
            for label, counter in (("Articles per year", Counter(r["year"] for r in rows if r["year"])),
                                   ("Publication types", Counter(t for r in rows for t in r["publication_types"])),
                                   ("Trial phases", Counter(r["trial_phase"] for r in rows if r["trial_phase"]))):
                s.append([])
                s.append([label])
                s.cell(s.max_row, 1).font = Font(bold=True)
                for k, v in sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:60]:
                    s.append([k, v])
            p = os.path.join(outdir, f"{stem}.xlsx")
            wb.save(p)
            paths.append(p)
    return paths


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
def safe_name(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w\-]+", "_", s.strip())).strip("_") or "drug"


def pubmed(drug_name: str,
           api_key: str | None = None,
           email: str | None = None,
           workers: int = 6,
           clinical_only: bool = False,
           use_synonyms: bool = True,
           max_synonyms: int | None = None,
           raw_synonyms: bool = False,
           keep_all: bool = False,
           extra_terms: list[str] | None = None,
           formats: tuple[str, ...] = ("csv", "json", "xlsx"),
           outdir: str = ".") -> dict:
    """
    Fetch all PubMed data for `drug_name` (parallelised) and write output files.

        from pubmed import pubmed
        result = pubmed("Semaglutide")

    Returns a dict:
        {"drug", "articles": [<record dicts>], "files": [<paths>], "metadata": {...},
         "failed_pmids": [...]}
    Raises ValueError for empty input and RuntimeError if the search fails.
    Returns an empty "articles" list (and no files) if nothing matches.
    """
    drug = (drug_name or "").strip()
    if not drug:
        raise ValueError("drug_name is required")
    api_key = api_key or os.getenv("NCBI_API_KEY")
    email = email or os.getenv("NCBI_EMAIL")
    extra_terms = list(extra_terms or [])

    t_start = time.time()
    client = NCBIClient(api_key, email)

    def empty(msg: str, meta=None) -> pd.DataFrame:
        print(f"  {msg}")
        df = pd.DataFrame(columns=["Trial_ID", "Registry", "Source"])
        df.attrs.update(articles=[], files=[], metadata=meta or {}, failed_pmids=[])
        return df

    print(f"\n{'=' * 64}\n  PubMed fetch for: {drug}\n"
          f"  API key: {'yes (10 req/s)' if api_key else 'no (3 req/s)'} | workers: {workers}\n{'=' * 64}")

    # 1. Search terms
    terms = [drug] + extra_terms
    syns: list[str] = []
    if use_synonyms:
        if _fetch_search_terms is not None:
            print("\n[1/4] Expanding synonyms via fetch_search_terms...")
            try:
                fetched = _fetch_search_terms(drug)
                syns = [t for t in fetched if t.lower() != drug.lower()]
                terms += syns
                print(f"  + {len(syns)} synonyms: {syns}")
            except Exception as e:
                print(f"  fetch_search_terms failed ({e}), falling back to PubChem...")
                syns = get_synonyms(drug, max_synonyms or None, raw_synonyms)
                terms += syns
                print(f"  + {len(syns)} synonyms: {syns}")
        else:
            print("\n[1/4] Expanding synonyms via PubChem (fetch_search_terms unavailable)...")
            syns = get_synonyms(drug, max_synonyms or None, raw_synonyms)
            terms += syns
            print(f"  + {len(syns)} synonyms: {syns}")
    else:
        print("\n[1/4] Synonym expansion skipped")
    terms = list(dict.fromkeys(t for t in terms if t))

    # 2. Search
    print("\n[2/4] Searching PubMed...")
    queries = build_queries(terms, clinical_only)
    print(f"  {len(terms)} search terms -> {len(queries)} query chunk(s)")
    try:
        pmids = search_all(client, queries, workers)
    except Exception as e:
        raise RuntimeError(f"PubMed search failed: {e}") from e
    if not pmids:
        return empty(f"No PubMed articles found for '{drug}'.")
    print(f"  {len(pmids):,} unique PMIDs to fetch")

    # 3. Fetch + parse in parallel
    print(f"\n[3/4] Fetching article records ({FETCH_BATCH}/batch, {workers} threads)...")
    rows, failed = fetch_all(client, pmids, workers)
    if failed:
        print(f"  Retrying {len(failed)} PMIDs from failed batches (single thread)...")
        retry_rows, failed = fetch_all(client, failed, 1)
        rows.extend(retry_rows)

    rows = list({r["pmid"]: r for r in rows}.values())            # de-dupe
    rows.sort(key=lambda r: r["publication_date"], reverse=True)  # newest first

    n_fetched = len(rows)
    if not keep_all:
        rows = [r for r in rows if r["trial_registry_ids"]]
        print(f"  Kept {len(rows):,} of {n_fetched:,} articles that have a trial registry ID "
              f"({n_fetched - len(rows):,} dropped)")
    meta = {
        "drug": drug,
        "search_terms": terms,
        "queries": queries,
        "synonyms_used": len(syns),
        "pmids_found": len(pmids),
        "articles_fetched_before_filter": n_fetched,
        "only_with_trial_registry_id": not keep_all,
        "clinical_only": clinical_only,
        "articles_parsed": len(rows),
        "failed_pmids": len(failed),
        "pulled_on": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "NCBI E-utilities (PubMed)",
    }
    if not rows:
        return empty("No articles with trial registry IDs were found.", meta)

    # 4. Write
    print("\n[4/4] Writing output...")
    paths = write_outputs(rows, outdir, f"{safe_name(drug)}_pubmed", list(formats), meta)

    years = Counter(r["year"] for r in rows if r["year"])
    print(f"\n{'=' * 64}\n  DONE in {time.time() - t_start:0.1f}s")
    print(f"  Articles: {len(rows):,}  |  with abstract: {sum(1 for r in rows if r['abstract']):,}"
          f"  |  with DOI: {sum(1 for r in rows if r['doi']):,}"
          f"  |  with trial IDs: {sum(1 for r in rows if r['trial_registry_ids']):,}")
    if years:
        print(f"  Year range: {min(years)} - {max(years)}")
    if failed:
        print(f"  WARNING: {len(failed)} PMIDs could not be fetched: {failed[:10]}...")
    for p in paths:
        print(f"  -> {p}")
    print("=" * 64)

    # Build standard DataFrame: one row per unique trial ID found across all articles
    trial_rows: list[dict] = []
    seen_ids: set[str] = set()
    for article in rows:
        for trial_id, registry in article["trial_registry_ids"].items():
            if registry.lower() in _EXCLUDED_REGISTRIES:
                continue
            if trial_id not in seen_ids:
                seen_ids.add(trial_id)
                trial_rows.append({"Trial_ID": trial_id, "Registry": registry, "Source": "pubmed"})

    result = (pd.DataFrame(trial_rows) if trial_rows
              else pd.DataFrame(columns=["Trial_ID", "Registry", "Source"]))
    result.attrs.update(articles=rows, files=paths, metadata=meta, failed_pmids=failed)
    print(f"  Trial IDs extracted: {len(result)}")
    return result


main = pubmed   # alias, so `from pubmed import main; main("Semaglutide")` also works


def _cli():
    ap = argparse.ArgumentParser(description="Fetch all PubMed data for a drug (parallelised).")
    ap.add_argument("drug", help="Drug name, e.g. Semaglutide (quote multi-word names)")
    ap.add_argument("--api-key", default=None, help="NCBI API key (or set NCBI_API_KEY env var)")
    ap.add_argument("--email", default=None, help="Contact email (or set NCBI_EMAIL)")
    ap.add_argument("--workers", type=int, default=6, help="Parallel threads (default 6)")
    ap.add_argument("--clinical-only", action="store_true",
                    help="Restrict to trials / RCTs / meta-analyses / systematic reviews / observational studies")
    ap.add_argument("--no-synonyms", action="store_true", help="Skip PubChem synonym expansion")
    ap.add_argument("--max-synonyms", type=int, default=0, help="Cap on synonyms (0 = no cap)")
    ap.add_argument("--raw-synonyms", action="store_true", help="Do not filter PubChem synonyms")
    ap.add_argument("--keep-all", action="store_true", help="Keep articles WITHOUT a trial registry ID")
    ap.add_argument("--extra-terms", nargs="*", default=[], help="Extra search terms")
    ap.add_argument("--formats", nargs="+", default=["csv", "json", "xlsx"],
                    choices=["csv", "json", "xlsx"], help="Output formats (default: all)")
    ap.add_argument("--outdir", default=".", help="Output directory (default: current)")
    a = ap.parse_args()
    try:
        pubmed(a.drug, api_key=a.api_key, email=a.email, workers=a.workers,
               clinical_only=a.clinical_only, use_synonyms=not a.no_synonyms,
               max_synonyms=a.max_synonyms or None, raw_synonyms=a.raw_synonyms,
               keep_all=a.keep_all, extra_terms=a.extra_terms,
               formats=tuple(a.formats), outdir=a.outdir)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    _cli()