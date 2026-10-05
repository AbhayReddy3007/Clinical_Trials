#!/usr/bin/env python3
"""
eudract.py – Fetch EU clinical trials from the legacy EU-CTR (EudraCT) register.
Usage (standalone): python eudract.py Semaglutide
Usage (as module):  from eudract import eudract; rows = eudract("Semaglutide")
"""

import argparse, csv, html as html_mod, json, re, sys, time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup

BASE        = "https://www.clinicaltrialsregister.eu"
SEARCH_URL  = BASE + "/ctr-search/search?query={query}&page={page}"
TRIAL_URL   = BASE + "/ctr-search/trial/{eudract}/{country}"
RESULTS_URL = BASE + "/ctr-search/trial/{eudract}/results"

HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}
PER_PAGE      = 20
TIMEOUT       = 60
RETRIES       = 3
RETRY_BACKOFF = 3
POLITE_DELAY  = 0.6


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(session: requests.Session, url: str) -> str:
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"GET {url} failed after {RETRIES} attempts: {last_exc}")


# ── Text helpers ──────────────────────────────────────────────────────────────

def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(text or "")).strip()


LABELS = [
    "EudraCT Number", "Sponsor Protocol Number", "Start Date", "Sponsor Name",
    "Full Title", "Medical condition", "Disease", "Population Age", "Gender",
    "Trial protocol", "Trial results",
]


def _field(block_text: str, label: str, stop_labels: List[str]) -> str:
    stops   = "|".join(re.escape(s) for s in stop_labels)
    pattern = re.escape(label) + r"\s*:?\s*(.*?)(?=" + (stops + "|$" if stops else "$") + ")"
    m       = re.search(pattern, block_text, flags=re.IGNORECASE | re.DOTALL)
    return _clean(m.group(1)) if m else ""


def _table_value(soup: BeautifulSoup, code_prefix: str) -> str:
    prefix = code_prefix.rstrip()
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) >= 2:
            first = _clean(cells[0].get_text(" ", strip=True))
            if first == prefix:
                return _clean(cells[-1].get_text(" ", strip=True))
            if first.startswith(prefix):
                rest = first[len(prefix):]
                if rest and rest[0] in ".0123456789":
                    continue
                return _clean(cells[-1].get_text(" ", strip=True))
    return ""


# ── Search result parsing ─────────────────────────────────────────────────────

def _parse_result_block(block) -> Optional[Dict[str, Any]]:
    text = _clean(block.get_text(" ", strip=True))
    if "EudraCT Number" not in text:
        return None

    def f(label):
        return _field(text, label, [l for l in LABELS if l != label])

    eudract_raw = f("EudraCT Number")
    m = re.match(r"(\d{4}-\d{6}-\d{2})", eudract_raw)
    eudract_num = m.group(1) if m else eudract_raw
    if not eudract_num:
        return None

    countries, statuses = [], []
    for a in block.find_all("a", href=True):
        cm = re.search(r"/ctr-search/trial/[\d-]+/([A-Za-z0-9]+)", a["href"])
        if cm and "results" not in a["href"]:
            code = cm.group(1)
            if code not in countries:
                countries.append(code)
    for sm in re.finditer(r"\(([^()]{3,40})\)",
                          _field(text, "Trial protocol", ["Trial results"])):
        s = sm.group(1).strip()
        if s not in statuses:
            statuses.append(s)

    has_results = "view results" in text.lower()
    return {
        "eudract_number":        eudract_num,
        "sponsor_protocol_number": f("Sponsor Protocol Number"),
        "start_date":            f("Start Date").lstrip("*: ").strip(),
        "sponsor":               f("Sponsor Name"),
        "title":                 f("Full Title"),
        "medical_condition":     f("Medical condition"),
        "disease_meddra":        f("Disease"),
        "population_age":        f("Population Age"),
        "gender":                f("Gender"),
        "countries":             "; ".join(countries),
        "status":                "; ".join(statuses),
        "results_available":     "Yes" if has_results else "No",
        "results_url":           RESULTS_URL.format(eudract=eudract_num) if has_results else "",
        "url":                   TRIAL_URL.format(eudract=eudract_num,
                                                  country=countries[0] if countries else "GB"),
        "source":                "EudraCT",
    }


def _total_results(soup: BeautifulSoup) -> int:
    m = re.search(r"([\d,]+)\s+result\(s\)\s+found", soup.get_text(" ", strip=True))
    return int(m.group(1).replace(",", "")) if m else 0


# ── Search ────────────────────────────────────────────────────────────────────

def _search_trials(drug: str, session: requests.Session,
                   max_records: Optional[int] = None,
                   verbose: bool = True) -> Iterator[Dict[str, Any]]:
    page, yielded, total = 1, 0, None
    query = quote_plus(drug)
    while True:
        html_text = _get(session, SEARCH_URL.format(query=query, page=page))
        soup      = BeautifulSoup(html_text, "html.parser")
        if total is None:
            total = _total_results(soup)
            if verbose:
                print(f"  EudraCT: {total} matching trial(s).", file=sys.stderr)
            if total == 0:
                return
        blocks = soup.select("table.result") or soup.find_all("table")
        found  = 0
        for block in blocks:
            row = _parse_result_block(block)
            if not row:
                continue
            found += 1
            yield row
            yielded += 1
            if max_records and yielded >= max_records:
                return
        if found == 0 or yielded >= total:
            return
        page += 1
        time.sleep(POLITE_DELAY)


# ── Protocol detail ───────────────────────────────────────────────────────────

def _get_trial_details(eudract_num: str, country: str,
                       session: requests.Session) -> Dict[str, Any]:
    page = _get(session, TRIAL_URL.format(eudract=eudract_num, country=country))
    soup = BeautifulSoup(page, "html.parser")

    def tv(code):
        return _table_value(soup, code)

    out: Dict[str, Any] = {
        # A – Protocol
        "full_title":        tv("A.3 ") or tv("A.3"),
        "lay_title":         tv("A.3.1"),
        "trial_is_pip":      tv("A.7"),
        # B – Sponsor
        "sponsor_name":      tv("B.1.1"),
        "sponsor_country":   tv("B.1.3.4"),
        "sponsor_status":    tv("B.3"),
        # D – IMP
        "product_name":      tv("D.3.1"),
        "product_code":      tv("D.3.2"),
        "pharmaceutical_form": tv("D.3.4 "),
        "route":             tv("D.3.7"),
        "inn":               tv("D.3.8"),
        "cas_number":        tv("D.3.9.1"),
        "strength":          tv("D.3.10.3"),
        "strength_unit":     tv("D.3.10.1"),
        "orphan_drug":       tv("D.2.5 "),
        "has_marketing_auth": tv("D.2.1"),
        "imp_role":          tv("D.1.2"),
        # E – General
        "medical_condition_full":    tv("E.1.1 "),
        "condition_lay":             tv("E.1.1.1"),
        "therapeutic_area":          tv("E.1.1.2"),
        "rare_disease":              tv("E.1.3"),
        "main_objective":            tv("E.2.1"),
        "secondary_objectives":      tv("E.2.2"),
        "primary_endpoint":          tv("E.5.1 "),
        "primary_endpoint_timeframe": tv("E.5.1.1"),
        "secondary_endpoint":        tv("E.5.2 "),
        "secondary_endpoint_timeframe": tv("E.5.2.1"),
        "inclusion_criteria":        tv("E.3"),
        "exclusion_criteria":        tv("E.4"),
        # E.6 – Scope
        "scope_diagnosis":     tv("E.6.1"),
        "scope_prophylaxis":   tv("E.6.2"),
        "scope_therapy":       tv("E.6.3"),
        "scope_safety":        tv("E.6.4"),
        "scope_efficacy":      tv("E.6.5"),
        "scope_pk":            tv("E.6.6"),
        "scope_pd":            tv("E.6.7"),
        "scope_bioequivalence": tv("E.6.8"),
        # E.8 – Design
        "controlled":           tv("E.8.1 "),
        "randomised":           tv("E.8.1.1"),
        "open_label":           tv("E.8.1.2"),
        "single_blind":         tv("E.8.1.3"),
        "double_blind":         tv("E.8.1.4"),
        "parallel_group":       tv("E.8.1.5"),
        "crossover":            tv("E.8.1.6"),
        "comparator_other":     tv("E.8.2.1"),
        "comparator_placebo":   tv("E.8.2.2"),
        "number_of_arms":       tv("E.8.2.4"),
        "sites_member_state":   tv("E.8.4.1"),
        "sites_eea":            tv("E.8.5.1"),
        "has_dmc":              tv("E.8.7"),
        # F – Population
        "subjects_member_state": tv("F.4.1"),
        "subjects_eea":          tv("F.4.2.1"),
        "subjects_worldwide":    tv("F.4.2.2"),
        "healthy_volunteers":    tv("F.3.1"),
        # Regulatory
        "ca_decision": "", "ca_decision_date": "",
        "ethics_opinion": "", "ethics_opinion_date": "",
        "end_of_trial_status": "", "global_end_date": "",
    }

    # Phase detection
    phases = []
    for desc_fragment, label in [
        ("Human pharmacology",    "Phase I"),
        ("Therapeutic exploratory", "Phase II"),
        ("Therapeutic confirmatory", "Phase III"),
        ("Therapeutic use",       "Phase IV"),
    ]:
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) >= 2:
                row_text = _clean(tr.get_text(" ", strip=True))
                if desc_fragment in row_text:
                    if _clean(cells[-1].get_text(" ", strip=True)).lower().startswith("yes"):
                        phases.append(label)
                    break
    out["phase"] = ", ".join(phases)

    # Regulatory / end-of-trial rows
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        lbl = _clean(cells[0].get_text())
        val = _clean(cells[-1].get_text())
        if "Competent Authority Decision" in lbl and "Date" not in lbl:
            out["ca_decision"] = val
        elif "Date of Competent Authority" in lbl:
            out["ca_decision_date"] = val
        elif "Ethics Committee Opinion" in lbl and "Date" not in lbl and "Reason" not in lbl:
            out["ethics_opinion"] = val
        elif "Date of Ethics Committee" in lbl:
            out["ethics_opinion_date"] = val
        elif "End of Trial Status" in lbl:
            out["end_of_trial_status"] = val
        elif "global end of the trial" in lbl.lower():
            out["global_end_date"] = val

    return out


# ── Results page ──────────────────────────────────────────────────────────────

def _get_trial_results(eudract_num: str, session: requests.Session) -> Dict[str, Any]:
    try:
        page = _get(session, RESULTS_URL.format(eudract=eudract_num))
    except Exception:
        return {}
    soup = BeautifulSoup(page, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    out: Dict[str, Any] = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        lbl = _clean(cells[0].get_text())
        val = _clean(cells[-1].get_text())
        if "Global end of trial date"              in lbl: out["results_global_end_date"]    = val
        elif "Actual start date of recruitment"    in lbl: out["results_recruitment_start"]  = val
        elif "Worldwide total number of subjects"  in lbl: out["results_subjects_worldwide"] = val
        elif "EEA total number of subjects"        in lbl: out["results_subjects_eea"]       = val
        elif "Main objective"                      in lbl: out["results_main_objective"]     = val
        elif "Analysis stage" in lbl and "Date" not in lbl: out["results_analysis_stage"]   = val
        elif "Date of interim/final analysis"      in lbl: out["results_analysis_date"]     = val

    findings_parts = []
    endpoint_blocks = re.findall(
        r"((?:Primary|Secondary)\s*:\s*.+?)(?=(?:Primary|Secondary)\s*:|Adverse events|More Information|$)",
        text, flags=re.IGNORECASE | re.DOTALL,
    )
    for block in endpoint_blocks:
        title_m = re.search(
            r"End point title\s+(.+?)(?:End point description|End point type|$)",
            block, re.IGNORECASE | re.DOTALL)
        title = _clean(title_m.group(1)) if title_m else ""

        desc_m = re.search(
            r"End point description\s+(.+?)(?:End point type|End point timeframe|$)",
            block, re.IGNORECASE | re.DOTALL)
        desc = _clean(desc_m.group(1)) if desc_m else ""

        stat_parts = []
        for pat in [
            r"p-value\s*[=:]\s*([\d.<>eE\-]+)",
            r"Confidence interval\s*[:]?\s*([\d\.\-\sto]+)",
            r"(?:mean|median)\s+difference\s*[=:]\s*([\d\.\-\sto]+)",
            r"(?:hazard|odds|risk)\s+ratio\s*[=:]\s*([\d\.\-\sto]+)",
            r"Statistical analysis\s+(.{10,300}?)(?:Notes|End point|$)",
        ]:
            for m in re.finditer(pat, block, re.IGNORECASE):
                stat_parts.append(m.group(0).strip()[:200])

        line = " | ".join(p for p in [title, desc] + stat_parts if p)
        if line:
            findings_parts.append(line[:500])

    for n in re.findall(r"Justification:\s*(.{10,500}?)(?:\n|Notes|$)", text, re.IGNORECASE):
        findings_parts.append(f"Note: {_clean(n)[:300]}")

    ae_m = re.search(
        r"Adverse event reporting additional description\s+(.{10,500}?)(?:Assessment type|Dictionary|$)",
        text, re.IGNORECASE | re.DOTALL)
    if ae_m:
        out["adverse_events_summary"] = _clean(ae_m.group(1))[:500]

    lim_m = re.search(
        r"Limitations of the trial\s+(?:such as.*?\.)\s*(.{10,800}?)(?:For support|$)",
        text, re.IGNORECASE | re.DOTALL)
    if lim_m:
        out["limitations"] = _clean(lim_m.group(1))[:500]

    out["findings"] = " ;; ".join(findings_parts)
    return out


# ── Public API ────────────────────────────────────────────────────────────────

def eudract(drug_name: str,
            details:     bool          = True,
            results:     bool          = True,
            max_records: Optional[int] = None,
            verbose:     bool          = True) -> List[Dict[str, Any]]:
    """
    Fetch clinical trials from the EudraCT register for *drug_name*.

    Parameters
    ----------
    drug_name   : search term (drug / INN / brand name)
    details     : if True, fetch full per-trial protocol detail page
    results     : if True, fetch results / findings page for completed trials
    max_records : stop after this many results (None = all)
    verbose     : print progress to stderr

    Returns
    -------
    List of flat dicts, one per trial.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    if verbose:
        print(f"[EudraCT] Searching for '{drug_name}' …", file=sys.stderr)

    rows: List[Dict[str, Any]] = []
    for i, row in enumerate(
            _search_trials(drug_name, session,
                           max_records=max_records,
                           verbose=verbose), start=1):
        if details:
            country = (row.get("countries", "").split(";")[0] or "GB").strip()
            try:
                row.update(_get_trial_details(row["eudract_number"], country, session))
            except Exception as exc:
                if verbose:
                    print(f"  ! detail fetch failed for {row['eudract_number']}: {exc}",
                          file=sys.stderr)
            time.sleep(POLITE_DELAY)

        if results and row.get("results_available") == "Yes":
            try:
                row.update(_get_trial_results(row["eudract_number"], session))
            except Exception as exc:
                if verbose:
                    print(f"  ! results fetch failed for {row['eudract_number']}: {exc}",
                          file=sys.stderr)
            time.sleep(POLITE_DELAY)

        rows.append(row)
        if verbose and i % 20 == 0:
            print(f"  … {i} trials processed", file=sys.stderr)

    if verbose:
        print(f"[EudraCT] Done – {len(rows)} trial(s) fetched.", file=sys.stderr)
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
    ap = argparse.ArgumentParser(description="Fetch EU clinical trials from EudraCT.")
    ap.add_argument("drug")
    ap.add_argument("--no-details", dest="details", action="store_false",
                    help="Skip per-trial protocol detail fetch")
    ap.add_argument("--no-results", dest="results", action="store_false",
                    help="Skip per-trial results page fetch")
    ap.add_argument("--max-records", type=int, default=None)
    ap.add_argument("--out",    default=None)
    ap.add_argument("--format", choices=("csv", "json", "both"), default="both")
    args = ap.parse_args()

    prefix = args.out or f"{args.drug.lower().replace(' ', '_')}_eudract"
    rows   = eudract(args.drug, details=args.details,
                     results=args.results, max_records=args.max_records)

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
        print(f"  {row['eudract_number']}  [Phase {row.get('phase', '')}] "
              f"[{row.get('status', '')}]  {row.get('title', '')[:70]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())