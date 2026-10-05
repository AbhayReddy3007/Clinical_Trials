#!/usr/bin/env python3
"""
ctis.py – Fetch EU clinical trials from the CTIS public portal.
Usage (standalone): python ctis.py Semaglutide
Usage (as module):  from ctis import ctis; rows = ctis("Semaglutide")
"""

import argparse, csv, json, sys, time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote
import requests

BASE        = "https://euclinicaltrials.eu"
SEARCH_URL  = f"{BASE}/ctis-public-api/search"
RETRIEVE_URL = f"{BASE}/ctis-public-api/retrieve/{{ct_number}}"
TRIAL_PAGE  = f"{BASE}/ctis-public/search?lang=en&EUCT={{ct_number}}"

HEADERS = {
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin":       BASE,
    "Referer":      f"{BASE}/ctis-public/search",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}
TIMEOUT       = 60
RETRIES       = 3
RETRY_BACKOFF = 3


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _request(session: requests.Session, method: str, url: str, **kw):
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kw)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"Request to {url} failed after {RETRIES} attempts: {last_exc}")


def _search_page(session: requests.Session, criteria: dict, page: int, size: int):
    body = {
        "pagination":    {"page": page, "size": size},
        "sort":          {"property": "decisionDate", "direction": "DESC"},
        "searchCriteria": criteria,
    }
    resp = _request(session, "POST", SEARCH_URL, data=json.dumps(body))
    if resp.ok:
        return resp.json()
    # Fallback to GET
    url = (
        f"{SEARCH_URL}"
        f"?paging={quote(json.dumps({'page': page, 'size': size}))}"
        f"&searchCriteria={quote(json.dumps(criteria))}"
        f"&sort={quote(json.dumps({'property': 'decisionDate', 'direction': 'DESC'}))}"
    )
    resp = _request(session, "GET", url)
    resp.raise_for_status()
    return resp.json()


# ── Search ────────────────────────────────────────────────────────────────────

def _search_trials(drug: str, session: requests.Session,
                   page_size: int = 50, max_records: Optional[int] = None,
                   verbose: bool = True) -> Iterator[Dict[str, Any]]:
    criteria = {
        "containAll": drug, "containAny": None, "containNot": None,
        "title": None, "number": None, "status": None,
        "medicalCondition": None, "sponsor": None, "productName": None,
        "trialPhaseCode": None, "msc": None, "ageGroupCode": None,
        "therapeuticAreaCode": None, "gender": None, "eudraCtCode": None,
        "trialRegion": None,
    }
    page, yielded = 1, 0
    while True:
        payload    = _search_page(session, criteria, page, page_size)
        records    = payload.get("data") or []
        pagination = payload.get("pagination") or {}
        if verbose and page == 1:
            total = pagination.get("totalRecords", len(records))
            print(f"  CTIS: {total} matching trial(s).", file=sys.stderr)
        for rec in records:
            yield rec
            yielded += 1
            if max_records and yielded >= max_records:
                return
        if not pagination.get("nextPage") or not records:
            return
        page += 1
        time.sleep(0.4)


def _get_trial_details(ct_number: str, session: requests.Session) -> Dict[str, Any]:
    resp = _request(session, "GET", RETRIEVE_URL.format(ct_number=ct_number))
    resp.raise_for_status()
    return resp.json()


# ── Flatten ───────────────────────────────────────────────────────────────────

def _as_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val] if val else []
    return [str(val)]


def _safe(d, *keys, default=""):
    v = d
    for k in keys:
        if isinstance(v, dict):
            v = v.get(k)
        else:
            return default
    return v if v is not None else default


def _part_one(details: Dict[str, Any]) -> Dict[str, Any]:
    return _safe(details, "authorizedApplication", "authorizedPartI", default={})


def _extract_products(details: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for prod in _safe(_part_one(details), "products", default=[]) or []:
        info = prod.get("productDictionaryInfo") or {}
        out.append({
            "product_name":       info.get("prodName") or prod.get("productName") or "",
            "active_substance":   info.get("activeSubstanceName") or "",
            "atc_code":           info.get("atcCode") or "",
            "pharmaceutical_form": info.get("pharmForm") or "",
            "route":              ", ".join(_as_list(prod.get("routes"))),
            "strength":           info.get("strength") or "",
            "imp_role":           prod.get("impRole") or "",
            "orphan_drug":        str(prod.get("orphanDrug", "")),
            "has_marketing_auth": str(prod.get("hasMarketingAuth", "")),
        })
    return out


def _extract_sponsors(details: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for sp in _safe(_part_one(details), "sponsors", default=[]) or []:
        org = sp.get("organisation") or {}
        out.append({
            "name":    org.get("name", ""),
            "country": org.get("countryName", ""),
            "status":  sp.get("sponsorType", ""),
        })
    return out


def _extract_results_summary(details: Dict[str, Any]) -> str:
    results = _safe(details, "results", default={}) or {}
    if not results:
        results = _safe(details, "authorizedApplication", "results", default={}) or {}
    summaries = []
    for ep in _safe(results, "endpoints", default=[]) or []:
        title      = ep.get("title") or ep.get("name") or ""
        ep_type    = ep.get("type") or ""
        desc       = ep.get("description") or ""
        stat       = ep.get("statisticalAnalysis") or ep.get("result") or ""
        conclusion = ep.get("conclusion") or ""
        parts = [p for p in [f"[{ep_type}]" if ep_type else "",
                              title, desc, stat, conclusion] if p]
        if parts:
            summaries.append(" | ".join(parts))
    overall = _safe(results, "summaryOfResults") or _safe(results, "overallConclusion") or ""
    if overall:
        summaries.insert(0, f"Overall: {overall}")
    return " ;; ".join(summaries)


def _flatten(summary: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ct_number = summary.get("ctNumber", "")
    row = {
        "ct_number":            ct_number,
        "eudract_number":       summary.get("eudraCtCode") or "",
        "title":                summary.get("ctTitle", ""),
        "short_title":          summary.get("shortTitle", ""),
        "status":               summary.get("ctStatus", ""),
        "phase":                summary.get("trialPhase", ""),
        "conditions":           summary.get("conditions", ""),
        "sponsor":              summary.get("sponsor", ""),
        "sponsor_type":         summary.get("sponsorType", ""),
        "product":              summary.get("product", ""),
        "countries":            "; ".join(_as_list(summary.get("trialCountries"))),
        "therapeutic_areas":    "; ".join(_as_list(summary.get("therapeuticAreas"))),
        "age_group":            summary.get("ageGroup", ""),
        "gender":               summary.get("gender", ""),
        "enrolled":             summary.get("totalNumberEnrolled", ""),
        "primary_endpoint":     summary.get("primaryEndPoint", ""),
        "secondary_endpoint":   summary.get("secondaryEndPoint", ""),
        "decision_date":        summary.get("decisionDateOverall", ""),
        "start_date":           (summary.get("startDate")
                                 or summary.get("startDateEU")
                                 or summary.get("decisionDateOverall") or ""),
        "end_date":             summary.get("endDate") or "",
        "results_first_received": summary.get("resultsFirstReceived", ""),
        "last_updated":         summary.get("lastUpdated", ""),
        "trial_region":         "; ".join(_as_list(summary.get("trialRegion"))),
        "msc":                  summary.get("msc", ""),
        "url":                  TRIAL_PAGE.format(ct_number=ct_number),
        "source":               "CTIS",
    }

    if details:
        products = _extract_products(details)
        sponsors = _extract_sponsors(details)

        row["product_names"]        = "; ".join(sorted({p["product_name"]       for p in products if p["product_name"]}))
        row["active_substances"]    = "; ".join(sorted({p["active_substance"]   for p in products if p["active_substance"]}))
        row["atc_codes"]            = "; ".join(sorted({p["atc_code"]           for p in products if p["atc_code"] and p["atc_code"] != "-"}))
        row["routes"]               = "; ".join(sorted({p["route"]              for p in products if p["route"]}))
        row["pharmaceutical_forms"] = "; ".join(sorted({p["pharmaceutical_form"] for p in products if p["pharmaceutical_form"]}))
        row["strengths"]            = "; ".join(sorted({p["strength"]           for p in products if p["strength"]}))
        row["imp_roles"]            = "; ".join(sorted({p["imp_role"]           for p in products if p["imp_role"]}))
        row["orphan_drug"]          = "; ".join(sorted({p["orphan_drug"]        for p in products if p["orphan_drug"]}))
        row["has_marketing_auth"]   = "; ".join(sorted({p["has_marketing_auth"] for p in products if p["has_marketing_auth"]}))
        row["sponsors_full"]        = "; ".join(sorted({s["name"]    for s in sponsors if s["name"]}))
        row["sponsor_countries"]    = "; ".join(sorted({s["country"] for s in sponsors if s["country"]}))

        p1 = _part_one(details)
        row["main_objective"]          = _safe(p1, "trialInformation", "mainObjective")
        row["secondary_objectives"]    = _safe(p1, "trialInformation", "secondaryObjectives")
        row["inclusion_criteria"]      = _safe(p1, "trialInformation", "inclusionCriteria")
        row["exclusion_criteria"]      = _safe(p1, "trialInformation", "exclusionCriteria")
        row["trial_design"]            = _safe(p1, "trialInformation", "trialDesign")
        row["comparator"]              = _safe(p1, "trialInformation", "comparator")
        row["planned_subjects_eea"]    = _safe(p1, "trialInformation", "numberSubjectsEEA")
        row["planned_subjects_worldwide"] = _safe(p1, "trialInformation", "numberSubjectsWorldwide")
        row["findings"]                = _extract_results_summary(details)

    return row


# ── Public API ────────────────────────────────────────────────────────────────

def ctis(drug_name: str,
         details:     bool          = True,
         max_records: Optional[int] = None,
         page_size:   int           = 50,
         verbose:     bool          = True) -> List[Dict[str, Any]]:
    """
    Fetch clinical trials from the CTIS portal for *drug_name*.

    Parameters
    ----------
    drug_name   : search term (drug / INN / brand name)
    details     : if True, fetch full per-trial detail records
    max_records : stop after this many results (None = all)
    page_size   : results per search page (max 50)
    verbose     : print progress to stderr

    Returns
    -------
    List of flat dicts, one per trial.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    if verbose:
        print(f"[CTIS] Searching for '{drug_name}' …", file=sys.stderr)

    rows: List[Dict[str, Any]] = []
    for i, summary in enumerate(
            _search_trials(drug_name, session,
                           page_size=page_size,
                           max_records=max_records,
                           verbose=verbose), start=1):
        det = None
        if details:
            try:
                det = _get_trial_details(summary.get("ctNumber", ""), session)
            except Exception as exc:
                if verbose:
                    print(f"  ! detail fetch failed for {summary.get('ctNumber')}: {exc}",
                          file=sys.stderr)
            time.sleep(0.3)
        rows.append(_flatten(summary, det))
        if verbose and i % 25 == 0:
            print(f"  … {i} trials processed", file=sys.stderr)

    if verbose:
        print(f"[CTIS] Done – {len(rows)} trial(s) fetched.", file=sys.stderr)
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────────

def _write_csv(rows: List[Dict], path: str):
    if not rows:
        return
    fieldnames: List[str] = []
    for row in rows:
        for k in row:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(rows: List[Dict], path: str):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description="Fetch EU/EEA clinical trials from CTIS.")
    ap.add_argument("drug")
    ap.add_argument("--no-details", dest="details", action="store_false",
                    help="Skip per-trial detail fetch (faster)")
    ap.add_argument("--max-records", type=int, default=None)
    ap.add_argument("--page-size",   type=int, default=50)
    ap.add_argument("--out",    default=None)
    ap.add_argument("--format", choices=("csv", "json", "both"), default="both")
    args = ap.parse_args()

    prefix = args.out or f"{args.drug.lower().replace(' ', '_')}_ctis"
    rows   = ctis(args.drug, details=args.details,
                  max_records=args.max_records, page_size=args.page_size)

    if not rows:
        print("No trials found.", file=sys.stderr)
        return 0

    if args.format in ("csv", "both"):
        _write_csv(rows, f"{prefix}.csv")
        print(f"Wrote {len(rows)} trials → {prefix}.csv", file=sys.stderr)
    if args.format in ("json", "both"):
        _write_json(rows, f"{prefix}.json")
        print(f"Wrote {len(rows)} trials → {prefix}.json", file=sys.stderr)

    for row in rows[:5]:
        print(f"  {row['ct_number']}  [Phase {row['phase']}] "
              f"[{row['status']}]  {row['title'][:70]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())