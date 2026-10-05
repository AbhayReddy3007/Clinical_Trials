#!/usr/bin/env python3
"""
ct.py - Fetch clinical trials from ClinicalTrials.gov for a drug.

Uses the public ClinicalTrials.gov REST API v2 (no API key required).
Expands the drug name into synonyms via fetch_search_terms, searches each
term, and returns a deduplicated DataFrame with Trial_ID | Registry | Source.

USAGE (import):
    from ct import ct
    df = ct("Semaglutide")   # -> DataFrame with Trial_ID | Registry | Source

USAGE (command line):
    python ct.py Semaglutide
    python ct.py Semaglutide --no-fetch-terms
    python ct.py Semaglutide --max-per-term 500

INSTALL:
    pip install requests pandas
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import pandas as pd
except ImportError:
    sys.exit("pip install pandas")

try:
    from fetch_search_terms import fetch_search_terms as _fetch_search_terms
except ImportError:
    _fetch_search_terms = None

# ── API constants ──────────────────────────────────────────────────────────────
API_BASE   = "https://clinicaltrials.gov/api/v2/studies"
PAGE_SIZE  = 1000   # max allowed by the API
TIMEOUT    = 30
REGISTRY   = "ClinicalTrials.gov"
SOURCE     = "Clinical Trials"


# ── HTTP ───────────────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "ct_fetcher/1.0 (clinical trial research)",
        "Accept": "application/json",
    })
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ── Search ─────────────────────────────────────────────────────────────────────
def _search_term(
    session: requests.Session,
    term: str,
    max_results: int = 0,
    delay: float = 0.5,
) -> list[str]:
    """Paginated NCT ID search. Returns list of NCT IDs."""
    nct_ids: list[str] = []
    params: dict = {
        "query.term": term,
        "pageSize": PAGE_SIZE,
        "fields": "NCTId",
        "format": "json",
    }

    page = 0
    next_token: str | None = None

    while True:
        if next_token:
            params["pageToken"] = next_token
        elif "pageToken" in params:
            del params["pageToken"]

        try:
            r = session.get(API_BASE, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            print(f"  [ct] request failed (term='{term}', page={page}): {e}")
            break
        except ValueError as e:
            print(f"  [ct] JSON parse error (term='{term}', page={page}): {e}")
            break

        studies = data.get("studies") or []
        for study in studies:
            nct_id = (study.get("protocolSection", {})
                          .get("identificationModule", {})
                          .get("nctId", "")).strip()
            if nct_id:
                nct_ids.append(nct_id)

        page += 1
        next_token = data.get("nextPageToken")

        if not next_token:
            break
        if max_results and len(nct_ids) >= max_results:
            break

        time.sleep(delay)

    return nct_ids


_XREF_BATCH = 100   # NCT IDs per secondary-ID lookup request


def _fetch_xrefs(
    session: requests.Session,
    nct_ids: list[str],
    delay: float = 0.5,
) -> dict[str, str]:
    """
    Fetch secondary/sponsor IDs for *nct_ids* in batches.
    Returns {secondary_id: nct_id} for use in cross-registry deduplication.
    Fails silently per batch so a bad field name never blocks the main results.
    """
    xrefs: dict[str, str] = {}
    for i in range(0, len(nct_ids), _XREF_BATCH):
        batch = nct_ids[i : i + _XREF_BATCH]
        try:
            params = {
                "filter.ids": ",".join(batch),
                "fields": "NCTId,SecondaryIdInfos",
                "pageSize": _XREF_BATCH,
                "format": "json",
            }
            r = session.get(API_BASE, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            for study in (r.json().get("studies") or []):
                id_mod = (study.get("protocolSection", {})
                               .get("identificationModule", {}))
                nct_id = id_mod.get("nctId", "").strip()
                if not nct_id:
                    continue
                for sid_info in id_mod.get("secondaryIdInfos", []):
                    sid = sid_info.get("id", "").strip()
                    if sid:
                        xrefs[sid] = nct_id
            time.sleep(delay)
        except Exception as e:
            print(f"  [ct] secondary-ID batch {i // _XREF_BATCH + 1} failed: {e}")
    return xrefs


# ── Main entry point ───────────────────────────────────────────────────────────
def ct(
    drug_name: str,
    fetch_terms: bool = True,
    max_per_term: int = 0,
    delay: float = 0.5,
    workers: int = 6,
) -> pd.DataFrame:
    """
    Fetch all ClinicalTrials.gov trials for *drug_name* and return a DataFrame
    with columns: Trial_ID | Registry | Source.

        from ct import ct
        df = ct("Semaglutide")

    Source is always "Clinical Trials".
    Automatically expands drug_name into synonyms via fetch_search_terms.
    """
    drug = " ".join(str(drug_name or "").split())
    if not drug:
        raise ValueError("drug_name is required")

    # 1. Build search-term list
    terms = [drug]
    if fetch_terms and _fetch_search_terms is not None:
        try:
            fetched = _fetch_search_terms(drug)
            seen = {drug.lower()}
            for t in fetched:
                if t.lower() not in seen:
                    seen.add(t.lower())
                    terms.append(t)
        except Exception as e:
            print(f"  [ct] term lookup failed ({e}) — searching only '{drug}'")
    print(f"  [ct] search terms: {terms}")

    session  = _make_session()
    seen_ids: set[str] = set()
    rows: list[dict]   = []
    _lock = threading.Lock()

    # 2. Search all terms in parallel (each term → paginated NCT ID list)
    def _fetch(term: str) -> tuple[str, list[str]]:
        return term, _search_term(session, term, max_results=max_per_term, delay=delay)

    with ThreadPoolExecutor(max_workers=min(workers, len(terms)),
                            thread_name_prefix="ct-term") as pool:
        futures = {pool.submit(_fetch, t): t for t in terms}
        for fut in as_completed(futures):
            term, nct_ids = fut.result()
            new = 0
            with _lock:
                for nct_id in nct_ids:
                    if nct_id not in seen_ids:
                        seen_ids.add(nct_id)
                        rows.append({"Trial_ID": nct_id, "Registry": REGISTRY, "Source": SOURCE})
                        new += 1
            print(f"  [ct] '{term}': {len(nct_ids)} hits, {new} new (total: {len(rows)})")

    if not rows:
        print("  [ct] no trials found.")
        return pd.DataFrame(columns=["Trial_ID", "Registry", "Source"])

    # 3. Fetch secondary IDs for cross-registry deduplication (separate batch calls)
    all_nct_ids = [r["Trial_ID"] for r in rows]
    print(f"  [ct] fetching secondary IDs for {len(all_nct_ids)} trial(s)…")
    xrefs = _fetch_xrefs(session, all_nct_ids, delay=delay)
    print(f"  [ct] {len(xrefs)} secondary-ID cross-reference(s) found.")

    result = pd.DataFrame(rows)
    result.attrs["xrefs"] = xrefs
    print(f"  [ct] {len(result)} unique trials found across {len(terms)} search term(s).")
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────
def _cli():
    ap = argparse.ArgumentParser(
        description="Fetch ClinicalTrials.gov trials for a drug and print Trial_ID | Registry | Source.")
    ap.add_argument("drug", help="Drug name, e.g. Semaglutide")
    ap.add_argument("--no-fetch-terms", action="store_true",
                    help="Skip synonym expansion; search only the given name")
    ap.add_argument("--max-per-term", type=int, default=0,
                    help="Cap results per search term (0 = all; default 0)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds between paginated requests (default 0.5)")
    args = ap.parse_args()

    df = ct(args.drug, fetch_terms=not args.no_fetch_terms,
            max_per_term=args.max_per_term, delay=args.delay)
    if df.empty:
        sys.exit(1)
    print(df.to_string(index=False))


if __name__ == "__main__":
    _cli()
