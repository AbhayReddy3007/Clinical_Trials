"""
nice.py - Fetch ALL available NICE documents for a drug from the public
nice.org.uk site (no API key) and return every clinical-trial registry ID found.
Runs 3 parallel workers by default.

Requirements:  pip install requests beautifulsoup4 pypdf

As a module:
    from nice import nice            # function name == file name
    ids = nice("semaglutide")        # -> ['NCT01720446', 'NCT03574597', ...]
    ids = nice("tirzepatide OR mounjaro", workers=3, verbose=True)
    records = nice("semaglutide", return_records=True)   # full dicts instead of IDs
    # `main` is an alias:  from nice import main ; main("semaglutide")

As a script:
    python nice.py semaglutide
    python nice.py --ta TA875 TA1152 --out out.json --debug

Each worker waits DELAY seconds between its own requests (3 workers ~ 2 req/s).
Respects robots.txt. Check NICE's re-use terms before bulk use, and put your
contact details in UA below.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from urllib import robotparser

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from fetch_search_terms import fetch_search_terms as _fetch_search_terms
except ImportError:
    _fetch_search_terms = None

BASE = "https://www.nice.org.uk"
UA = "trial-id-research-script/1.0 (contact: you@example.com)"  # <- put your contact here
DELAY = 1.5                 # seconds between requests, per worker
MAX_PDF_MB = 60
MAX_HOPS = 2                # document -> linked page -> linked document
INDEX_CACHE = "nice_index_cache.json"
DEBUG = False
VERBOSE = False

SEARCH_URLS = [
    BASE + "/search?q={q}&ndt=Guidance&ngt=Technology+appraisal+guidance&pa={page}",
    BASE + "/guidance/published?q={q}&ndt=Guidance&ngt=Technology%20appraisal%20guidance&ps=50&pa={page}",
]
TA_HREF = re.compile(r"/guidance/((?:ta|hst)\d+)\b", re.I)

ID_PATTERNS = [
    re.compile(r"\bNCT\d{8}\b", re.I),
    re.compile(r"\bEUCTR\d{4}-\d{6}-\d{2}\b", re.I),
    re.compile(r"\b(?:EudraCT[^0-9]{0,15})?(\d{4}-\d{6}-\d{2})\b", re.I),
    re.compile(r"\bISRCTN\d{8}\b", re.I),
    re.compile(r"\bACTRN\d{14}\b", re.I),
    re.compile(r"\bCTRI/\d{4}/\d{2,3}/\d+\b", re.I),
    re.compile(r"\bChiCTR-?[A-Z]{0,5}-?\d{8,10}\b", re.I),
    re.compile(r"\bJRCT[A-Z]?\d{6,12}\b", re.I),
    re.compile(r"\bKCT\d{7}\b", re.I),
]
SOURCE_MAP = {"NCT": "ClinicalTrials.gov", "EUCT": "EudraCT", "ISRCTN": "ISRCTN",
              "ACTRN": "ANZCTR", "CTRI": "CTRI (India)", "CHICTR": "ChiCTR",
              "JRCT": "JRCT (Japan)", "KCT": "CRIS (Korea)"}
RECS = [("Not recommended", r"\b(?:is|are) not recommended\b"),
        ("Only in research", r"\bonly in research\b"),
        ("Optimised", r"\boptimi[sz]ed\b"),
        ("Recommended", r"\b(?:is|are) recommended\b|\brecommended as an option\b")]
PAGES = ["", "/evidence", "/documents/committee-papers", "/documents/final-scope",
         "/chapter/1-Recommendations", "/resources"]
SKIP_EXT = (".xlsx", ".xls", ".docx", ".doc", ".zip", ".png", ".jpg", ".pptx")


def log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr, flush=True)


def dbg(msg: str) -> None:
    if DEBUG:
        log(f"    [debug] {msg}")


# ── Registry helpers ─────────────────────────────────────────────────────────
def registry_source(tid: str) -> str:
    if re.fullmatch(r"EUCTR\d{4}-\d{6}-\d{2}", tid):
        return "EudraCT"
    p = re.split(r"[/\-]", tid.upper())[0]
    return next((v for k, v in SOURCE_MAP.items() if p.startswith(k)), "Unknown")


def scan_ids(text: str) -> List[str]:
    out, seen = [], set()
    for pat in ID_PATTERNS:
        for m in pat.finditer(text):
            norm = (m.group(m.lastindex) if m.lastindex else m.group(0)).upper()
            if re.fullmatch(r"\d{4}-\d{6}-\d{2}", norm):
                norm = "EUCTR" + norm
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except ImportError:
        log("  [pdf] pypdf not installed: pip install pypdf")
    except Exception as exc:
        log(f"  [pdf] parse failed: {exc}")
    return ""


# ── Thread-safe fetcher: one Session + one rate-limit clock per worker thread ─
class Fetcher:
    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self.local = threading.local()
        self.rp: Optional[robotparser.RobotFileParser] = None
        try:  # fetch robots.txt with our own UA (urllib's default UA is often blocked)
            r = requests.get(BASE + "/robots.txt", headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200:
                self.rp = robotparser.RobotFileParser()
                self.rp.parse(r.text.splitlines())
            else:
                log(f"[warn] robots.txt returned {r.status_code}; not enforcing it")
        except requests.RequestException as exc:
            log(f"[warn] could not read robots.txt ({exc}); not enforcing it")

    def _st(self):
        l = self.local
        if not hasattr(l, "s"):
            l.s = requests.Session()
            l.s.headers["User-Agent"] = UA
            l.last, l.status = 0.0, None
        return l

    @property
    def last_status(self):
        return self._st().status

    def get(self, url: str) -> Optional[requests.Response]:
        l = self._st()
        l.status = None
        if self.rp and not self.rp.can_fetch(UA, url):
            l.status = "robots"
            log(f"  [robots] disallowed: {url}")
            return None
        for attempt in range(3):
            wait = self.delay - (time.time() - l.last)
            if wait > 0:
                time.sleep(wait)
            l.last = time.time()
            try:
                r = l.s.get(url, timeout=90)
            except requests.RequestException as exc:
                l.status = f"net:{type(exc).__name__}"
                time.sleep(2 ** attempt)
                continue
            l.status = r.status_code
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            return r if r.ok else None
        return None


# ── Step 1: discover TAs for a molecule ──────────────────────────────────────
def _aliases(molecule: str) -> List[str]:
    return [a.strip().lower() for a in re.split(r"\s+OR\s+", molecule, flags=re.I) if a.strip()]


def discover_via_search(f: Fetcher, molecule: str, max_pages: int = 5) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for alias in _aliases(molecule):
        for tmpl in SEARCH_URLS:
            for page in range(1, max_pages + 1):
                url = tmpl.format(q=quote_plus(alias), page=page)
                r = f.get(url)
                if not r:
                    log(f"  [search] no response (status {f.last_status}): {url}")
                    break
                new = 0
                for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
                    m = TA_HREF.search(a["href"])
                    if m and m.group(1).upper() not in found:
                        found[m.group(1).upper()] = a.get_text(" ", strip=True)
                        new += 1
                if new == 0:
                    break
            if found:
                return found
    return found


def _probe(f: Fetcher, gid: str) -> Tuple[str, Optional[str]]:
    """Return (id, title) ; title '' = genuine 404 ; None = error (robots/network)."""
    r = f.get(f"{BASE}/guidance/{gid.lower()}")
    if r is None:
        return gid, ("" if f.last_status == 404 else None)
    h1 = BeautifulSoup(r.text, "html.parser").find("h1")
    return gid, (h1.get_text(" ", strip=True) if h1 else "")


def build_index(f: Fetcher, workers: int, max_ta: int = 1400, max_hst: int = 150) -> Dict[str, str]:
    """Crawl /guidance/ta1.. and hst1.. in parallel, cache {ID: title}. Resumable."""
    try:
        index: Dict[str, str] = json.load(open(INDEX_CACHE))
    except (FileNotFoundError, ValueError):
        index = {}
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for prefix, limit in (("TA", max_ta), ("HST", max_hst)):
            n = 1
            while n <= limit:
                ids = [f"{prefix}{i}" for i in range(n, min(n + 30, limit + 1))]
                todo = [g for g in ids if g not in index]
                for gid, title in pool.map(lambda g: _probe(f, g), todo):
                    if title is None:
                        errors += 1
                        log(f"  [index] {gid}: status {f.last_status}")
                    else:
                        index[gid] = title
                if errors >= 10:
                    json.dump(index, open(INDEX_CACHE, "w"))
                    raise RuntimeError("Repeated non-404 failures (robots.txt block or network?). Stopping.")
                json.dump(index, open(INDEX_CACHE, "w"))
                log(f"  [index] {ids[-1]} done ({sum(1 for t in index.values() if t)} guidance pages cached)")
                if n > 200 and all(not index.get(g) for g in ids):
                    break                       # ran past the end of the series
                n += 30
    return index


def discover_tas(f: Fetcher, molecule: str, workers: int) -> Dict[str, str]:
    found = discover_via_search(f, molecule)
    if found:
        return found
    log(f"[NICE] Search gave nothing; building title index with {workers} workers "
        f"(slow first time, cached in {INDEX_CACHE}) ...")
    index = build_index(f, workers)
    al = _aliases(molecule)
    return {g: t for g, t in index.items() if t and any(a in t.lower() for a in al)}


# ── Step 2: per-TA pages -> list of document URLs ────────────────────────────
def _is_doc_link(path: str, ta: str) -> bool:
    if not path.startswith(f"/guidance/{ta.lower()}/"):
        return False
    if path.endswith(SKIP_EXT) or "/chapter/" in path:
        return False
    return "pdf" in path or any(k in path for k in ("/evidence/", "/documents/", "/resources/"))


def _doc_links(soup: BeautifulSoup, ta: str) -> List[str]:
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a["href"]).split("#")[0]
        if _is_doc_link(urlparse(href).path.lower(), ta) and href not in out:
            out.append(href)
    return out


def scan_ta_pages(f: Fetcher, ta: str) -> Dict[str, Any]:
    root = f"{BASE}/guidance/{ta.lower()}"
    res: Dict[str, Any] = {"ta": ta, "url": root, "title": "", "published": "",
                           "rec_text": "", "texts": [], "docs": [], "seen": set()}
    for suffix in PAGES:
        r = f.get(root + suffix)
        if not r:
            dbg(f"{ta}{suffix} -> status {f.last_status}")
            continue
        res["seen"].add(root + suffix)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if suffix == "":
            h1 = soup.find("h1")
            res["title"] = h1.get_text(" ", strip=True) if h1 else ""
            m = re.search(r"Published:\s*([0-9]{1,2} \w+ \d{4})", text)
            res["published"] = m.group(1) if m else ""
        if "Recommendations" in suffix:
            res["rec_text"] = text
        res["texts"].append(text)
        links = [u for u in _doc_links(soup, ta) if u not in res["docs"]]
        res["docs"].extend(links)
        dbg(f"{ta}{suffix or '/'}: {len(text)} chars, +{len(links)} doc links")
    log(f"  {ta}: {res['title'][:70]} -> {len(res['docs'])} document links")
    return res


# ── Step 3: fetch one document (PDF or HTML) ─────────────────────────────────
def fetch_doc(f: Fetcher, ta: str, url: str) -> Dict[str, Any]:
    out = {"ta": ta, "url": url, "text": "", "children": [], "kind": ""}
    r = f.get(url)
    if not r:
        dbg(f"{url} -> status {f.last_status}")
        return out
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or r.content[:5] == b"%PDF-":
        if len(r.content) > MAX_PDF_MB * 1_000_000:
            dbg(f"too large, skipped: {url}")
            return out
        out["text"], out["kind"] = pdf_text(r.content), "pdf"
    elif "html" in ctype:
        soup = BeautifulSoup(r.text, "html.parser")
        out["text"], out["kind"] = soup.get_text(" ", strip=True), "html"
        out["children"] = _doc_links(soup, ta)
    else:
        dbg(f"unsupported content-type {ctype}: {url}")
        return out
    log(f"    {ta} {out['kind']}: {url.rsplit('/', 1)[-1][:60]} "
        f"({len(out['text'])} chars, {len(scan_ids(out['text']))} IDs)")
    return out


# ── Orchestration ────────────────────────────────────────────────────────────
def scan_nice_web(molecule: str = "", tas: Optional[List[str]] = None,
                  workers: int = 3, max_docs: int = 0) -> List[Dict[str, Any]]:
    f = Fetcher()
    targets = {t.upper(): "" for t in tas} if tas else discover_tas(f, molecule, workers)
    log(f"[NICE] {len(targets)} TA/HST to process: {', '.join(targets) or 'none'}")
    if not targets:
        return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Stage 1: TA pages (parallel across TAs)
        tas_data = {d["ta"]: d for d in pool.map(lambda t: scan_ta_pages(f, t), list(targets))}

        # Stage 2: every linked document (parallel across all documents), up to MAX_HOPS
        seen: Set[str] = set()
        pending: List[Tuple[str, str]] = []
        for ta, d in tas_data.items():
            for u in d["docs"]:
                if u not in seen:
                    seen.add(u)
                    pending.append((ta, u))
        for hop in range(MAX_HOPS):
            if max_docs:
                pending = pending[:max(0, max_docs - len(seen) + len(pending))]
            if not pending:
                break
            log(f"[NICE] Fetching {len(pending)} documents (hop {hop + 1}) ...")
            nxt: List[Tuple[str, str]] = []
            for doc in pool.map(lambda p: fetch_doc(f, p[0], p[1]), pending):
                td = tas_data[doc["ta"]]
                td.setdefault("doc_hits", []).append((doc["url"], scan_ids(doc["text"])))
                td["texts"].append(doc["text"])
                for c in doc["children"]:
                    if c not in seen:
                        seen.add(c)
                        nxt.append((doc["ta"], c))
            pending = nxt

    # Build records: one per (TA, trial ID), with the documents it was found in
    records: List[Dict[str, Any]] = []
    for ta, d in tas_data.items():
        rec = next((lbl for lbl, pat in RECS if re.search(pat, d["rec_text"], re.I)), "")
        found: Dict[str, List[str]] = {}
        for text in d["texts"][:len(PAGES)]:                     # the HTML pages
            for tid in scan_ids(text):
                found.setdefault(tid, []).append(d["url"])
        for url, ids in d.get("doc_hits", []):
            for tid in ids:
                found.setdefault(tid, []).append(url)
        for tid, urls in found.items():
            records.append({"trial_id": tid, "registry_source": registry_source(tid),
                            "ta_number": ta, "trial_title": d["title"][:300],
                            "phase": "Technology Appraisal", "phase_status": rec,
                            "trial_location": "UK", "published": d["published"],
                            "source_url": d["url"], "found_in": sorted(set(urls))})
    return records


def nice(drug_name: str = "", *, tas: Optional[List[str]] = None, workers: int = 3,
         max_docs: int = 0, return_records: bool = False, verbose: bool = False,
         debug: bool = False) -> Any:
    """Fetch all NICE trial IDs for a drug and return a DataFrame with Trial_ID|Registry|Source.

    Automatically expands drug_name into synonyms via fetch_search_terms.
    return_records=True returns the raw list of record dicts instead.
    """
    global VERBOSE, DEBUG
    VERBOSE, DEBUG = verbose or debug, debug
    if not drug_name and not tas:
        raise ValueError("give a drug name or tas=[...]")

    # Expand to synonyms so all brand names / codes are searched
    search_molecule = drug_name
    if drug_name and not tas and _fetch_search_terms is not None:
        try:
            terms = _fetch_search_terms(drug_name)
            if len(terms) > 1:
                search_molecule = " OR ".join(terms)
                log(f"[NICE] Expanded to {len(terms)} terms: {search_molecule[:100]}...")
        except Exception as e:
            log(f"[NICE] fetch_search_terms failed ({e}), searching only '{drug_name}'")

    records = scan_nice_web(search_molecule, tas, workers, max_docs)

    if return_records:
        return records

    # Build standard DataFrame
    rows, seen = [], set()
    for r in records:
        tid = r["trial_id"]
        if tid in seen:
            continue
        seen.add(tid)
        rows.append({
            "Trial_ID": tid,
            "Registry": r.get("registry_source", ""),
            "Source": "nice",
        })
    log(f"[NICE] Done: {len(rows)} unique trial ID(s) across "
        f"{len({r['ta_number'] for r in records})} TA(s).")

    if pd is not None:
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["Trial_ID", "Registry", "Source"])
    return sorted(seen)


main = nice  # alias: `from nice import main`


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("molecule", nargs="?", default="")
    ap.add_argument("--ta", nargs="+", help="known TA/HST numbers (skips discovery)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-docs", type=int, default=0, help="cap documents per run (0 = all)")
    ap.add_argument("--out", help="write full JSON records here")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    if not a.molecule and not a.ta:
        ap.error("give a molecule or --ta")
    recs = nice(a.molecule, tas=a.ta, workers=a.workers, max_docs=a.max_docs,
                return_records=True, verbose=True, debug=a.debug)
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(recs, fh, indent=2)
        print(f"Full records written to {a.out}", file=sys.stderr)
    print("\n".join(sorted({r["trial_id"] for r in recs})))