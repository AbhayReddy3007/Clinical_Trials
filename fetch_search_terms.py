#!/usr/bin/env python3
"""
fetch_search_terms.py - Build a list of search terms (names/synonyms) for a drug from public APIs.

USAGE
    python fetch_search_terms.py Semaglutide
    python fetch_search_terms.py Semaglutide --sources chembl,rxnorm,openfda,pubchem
    python fetch_search_terms.py Semaglutide --min-sources 2      # only terms found in >=2 sources
    python fetch_search_terms.py Semaglutide --terms-only         # comma-separated, for -s "..."
    python fetch_search_terms.py Semaglutide --json terms.json    # also save details as JSON

INSTALL
    pip install requests

SOURCES (all free, no API key)
    chembl  - ChEMBL molecule synonyms, typed (TRADE_NAME, RESEARCH_CODE, INN, USAN, ...)
    rxnorm  - NLM RxNorm: ingredient + brand names (BN) + synonyms
    openfda - FDA drug labels: brand / generic / substance names actually on US labels
    pubchem - PubChem synonyms (very noisy, heavily filtered; off by default)

IMPORT IT (main function has the same name as the file)
    from fetch_search_terms import fetch_search_terms
    terms = fetch_search_terms("Semaglutide")          # -> list of search-term strings

  who.py calls this automatically, so you rarely need to run it yourself.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = 30
DEFAULT_SOURCES = ["chembl", "rxnorm", "openfda"]
ALL_SOURCES = ["chembl", "rxnorm", "openfda", "pubchem"]
CATEGORY_ORDER = ["query", "generic_name", "trade_name", "research_code", "other"]


# ──────────────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "fetch_search_terms/1.0 (drug synonym lookup)",
                      "Accept": "application/json"})
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_json(session, url, params=None):
    """GET -> parsed JSON, or None for 404 / empty results. Raises on other errors."""
    r = session.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ──────────────────────────────────────────────────────────────────────────────
# NOISE FILTER
# ──────────────────────────────────────────────────────────────────────────────
_NOISE_PREFIX = re.compile(
    r"^(CHEMBL|SCHEMBL|DTXSID|DTXCID|AKOS|MFCD|HY-|CS-|BCP|UNII|BDBM|ZINC|GTPL|SB\d|EX-A|NSC[\s-]?\d|"
    r"AS-|BS-|DB\d|CID[\s-]?\d|AC\d|CCG-|KS-|Q\d{4,}|SMR|MLS|NCGC|AMY|BRD-|EC\s?\d|CAS[\s-]|DSSTox|"
    r"HMS\d|LS-|SY\d|ACM\d|AB\d{4,}|BP-|FT-|NS\d|BCPP|SCHEMBL|MolPort|STK|BBL|NCI|WLN)",
    re.I)
_CAS = re.compile(r"^\d{2,7}-\d{2}-\d$")
_UNII = re.compile(r"^[0-9A-Z]{10}$")
_DOSAGE = re.compile(r"\b(mg|mcg|µg|ml|iu|injection|injectable|tablet|tablets|solution|pen|oral|capsule|"
                     r"kit|cartridge|syringe|suspension|dose|doses)\b", re.I)
_BAD_CHARS = re.compile(r"[\[\]{}<>=;:/\\,@#$%^&*|~`\"]")


def is_plausible(term: str) -> bool:
    t = term.strip()
    if not (2 <= len(t) <= 40):
        return False
    if len(t.split()) > 4:
        return False
    if _BAD_CHARS.search(t) or _CAS.match(t) or _NOISE_PREFIX.match(t) or _DOSAGE.search(t):
        return False
    if _UNII.match(t) and re.search(r"\d", t) and t.upper() == t and " " not in t and len(t) == 10:
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    return True


def tidy(term: str) -> str:
    """Normalise display: 'OZEMPIC' -> 'Ozempic', keep codes like 'NN9535' as-is."""
    t = re.sub(r"\s+", " ", term).strip()
    if t.isupper() and len(t) > 4 and not re.search(r"\d", t):
        return t.title()
    return t


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE: ChEMBL
# ──────────────────────────────────────────────────────────────────────────────
CHEMBL_URL = "https://www.ebi.ac.uk/chembl/api/data/molecule/search.json"
_CHEMBL_TYPE = {
    "TRADE_NAME": "trade_name",
    "RESEARCH_CODE": "research_code",
    "INN": "generic_name", "USAN": "generic_name", "BAN": "generic_name", "JAN": "generic_name",
    "FDA": "generic_name", "USP": "generic_name", "MI": "generic_name", "BNF": "generic_name",
    "ATC": "other", "OTHER": "other",
}


def parse_chembl(data, query):
    """-> list[(term, category)]. Prefer molecules whose name/synonym exactly matches the query."""
    mols = (data or {}).get("molecules") or []
    q = query.lower()

    def names(m):
        out = {(m.get("pref_name") or "").lower()}
        out |= {(s.get("molecule_synonym") or "").lower() for s in m.get("molecule_synonyms") or []}
        return out

    exact = [m for m in mols if q in names(m)]
    chosen = exact or mols[:1]
    out = []
    for m in chosen:
        if m.get("pref_name"):
            out.append((m["pref_name"], "generic_name"))
        for s in m.get("molecule_synonyms") or []:
            syn = s.get("molecule_synonym")
            if syn:
                out.append((syn, _CHEMBL_TYPE.get((s.get("syn_type") or "").upper(), "other")))
    return out


def fetch_chembl(session, query):
    data = get_json(session, CHEMBL_URL, {"q": query, "limit": 10})
    return parse_chembl(data, query)


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE: RxNorm (NLM RxNav)
# ──────────────────────────────────────────────────────────────────────────────
RXNAV = "https://rxnav.nlm.nih.gov/REST"
_RX_TTY = {"IN": "generic_name", "PIN": "generic_name", "MIN": "generic_name",
           "BN": "trade_name", "SY": "other"}


def parse_rxnorm_related(data):
    out = []
    groups = ((data or {}).get("allRelatedGroup") or {}).get("conceptGroup") or []
    for g in groups:
        cat = _RX_TTY.get(g.get("tty"))
        if not cat:
            continue
        for c in g.get("conceptProperties") or []:
            if c.get("name"):
                out.append((c["name"], cat))
    return out


def fetch_rxnorm(session, query):
    data = get_json(session, f"{RXNAV}/rxcui.json", {"name": query, "search": 1})
    ids = ((data or {}).get("idGroup") or {}).get("rxnormId") or []
    if not ids:                                      # fall back to fuzzy match
        data = get_json(session, f"{RXNAV}/approximateTerm.json", {"term": query, "maxEntries": 1})
        cands = ((data or {}).get("approximateGroup") or {}).get("candidate") or []
        ids = [c["rxcui"] for c in cands[:1] if c.get("rxcui")]
    out = []
    for rxcui in ids[:1]:
        out += parse_rxnorm_related(get_json(session, f"{RXNAV}/rxcui/{rxcui}/allrelated.json"))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE: openFDA drug labels
# ──────────────────────────────────────────────────────────────────────────────
OPENFDA_URL = "https://api.fda.gov/drug/label.json"


def parse_openfda_counts(data, category):
    return [(r["term"], category) for r in (data or {}).get("results") or [] if r.get("term")]


def fetch_openfda(session, query):
    out = []
    search = f'(openfda.substance_name:"{query}" OR openfda.generic_name:"{query}")'
    for field, cat in (("openfda.brand_name.exact", "trade_name"),
                       ("openfda.generic_name.exact", "generic_name"),
                       ("openfda.substance_name.exact", "generic_name")):
        data = get_json(session, OPENFDA_URL, {"search": search, "count": field, "limit": 50})
        out += parse_openfda_counts(data, cat)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# SOURCE: PubChem (noisy; stricter filtering)
# ──────────────────────────────────────────────────────────────────────────────
PUBCHEM_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{}/synonyms/JSON"
_CODE = re.compile(r"^[A-Za-z]{1,6}[- ]?\d{2,}[A-Za-z0-9\-]{0,6}$")     # e.g. NN9535, NNC 0113-0217
_WORD = re.compile(r"^[A-Za-z][a-z]{3,19}$")                              # e.g. Ozempic


def parse_pubchem(data):
    infos = ((data or {}).get("InformationList") or {}).get("Information") or []
    out = []
    for info in infos[:1]:
        for syn in info.get("Synonym") or []:
            if _CODE.match(syn):
                out.append((syn, "research_code"))
            elif _WORD.match(syn) or _WORD.match(syn.title()) and syn.istitle():
                out.append((syn, "other"))
    return out[:200]


def fetch_pubchem(session, query):
    return parse_pubchem(get_json(session, PUBCHEM_URL.format(quote(query))))


FETCHERS = {"chembl": fetch_chembl, "rxnorm": fetch_rxnorm,
            "openfda": fetch_openfda, "pubchem": fetch_pubchem}


# ──────────────────────────────────────────────────────────────────────────────
# CLEANING HELPERS
# ──────────────────────────────────────────────────────────────────────────────
_DESCRIPTIVE = re.compile(
    r"\b(component|components|combination|combined|mixture|analog|analogue|derivative|"
    r"fragment|conjugate|of|with|and|in|for|plus|salt)\b", re.I)
_CODE_LIKE = re.compile(r"^([A-Za-z]{1,6})[\s\-]?(\d[\dA-Za-z\-]*)$")


_SALT_WORDS = {
    "calcium", "hemicalcium", "sodium", "potassium", "magnesium", "zinc", "lithium", "disodium",
    "hydrochloride", "hcl", "dihydrochloride", "hydrobromide", "bromide", "chloride",
    "mesylate", "mesilate", "besylate", "tosylate", "tartrate", "bitartrate", "maleate", "fumarate",
    "succinate", "citrate", "sulfate", "sulphate", "phosphate", "acetate", "nitrate", "lactate",
    "oxalate", "benzoate", "tromethamine", "meglumine", "hydrate", "monohydrate", "dihydrate",
    "trihydrate", "hemihydrate", "anhydrous", "free", "base", "salt",
}


def strip_salt(text: str) -> str:
    """'Orforglipron Calcium' -> 'Orforglipron';  'LY-3502970 HEMICALCIUM' -> 'LY-3502970'."""
    toks = text.split()
    while len(toks) > 1 and toks[-1].lower().strip(",.()") in _SALT_WORDS:
        toks.pop()
    return " ".join(toks)


def is_descriptive(term: str, query: str) -> bool:
    """ChEMBL sometimes files phrases like 'Semaglutide component of cagrisema' as TRADE_NAME.
    Real names are not sentences: reject multi-word terms that contain connector words or that
    simply embed the drug name inside a longer phrase."""
    t = term.strip()
    if " " not in t:
        return False
    if _DESCRIPTIVE.search(t):
        return True
    return query.lower() in t.lower() and t.lower() != query.lower()


def canonical_code(term: str):
    """'NN-9535' / 'Nn9535' / 'NN 9535' -> 'NN9535';  'NNC 0113-0217' / 'NNC-0113-0217' -> 'NNC0113-0217'.
    Returns None if the term doesn't look like a development code."""
    m = _CODE_LIKE.match(re.sub(r"\s+", " ", term.strip()))
    if not m:
        return None
    prefix, rest = m.groups()
    return (prefix + rest).upper()


# ──────────────────────────────────────────────────────────────────────────────
# MERGE
# ──────────────────────────────────────────────────────────────────────────────
def merge(query, per_source: dict) -> list:
    """per_source: {source: [(term, category)]} -> sorted list of term dicts."""
    base = strip_salt(query) or query            # 'Orforglipron Calcium' -> 'Orforglipron'
    merged = {query.lower(): {"term": query, "category": "query", "sources": ["input"], "variants": []}}
    if base.lower() != query.lower():            # always search the salt-free name too
        merged[base.lower()] = {"term": base, "category": "generic_name",
                                "sources": ["derived"], "variants": []}
    rank = {c: i for i, c in enumerate(CATEGORY_ORDER)}

    for source, pairs in per_source.items():
        for raw, cat in pairs:
            if cat in ("generic_name", "research_code", "other"):
                raw = strip_salt(raw) or raw      # drop salt-form suffixes (HEMICALCIUM, SODIUM, ...)
            if not is_plausible(raw) or is_descriptive(raw, base):
                continue
            term, variant = tidy(raw), None
            if cat == "research_code":
                canon = canonical_code(term)
                if canon:
                    variant, term = term, canon
            key = term.lower()
            entry = merged.get(key)
            if entry is None:
                entry = merged[key] = {"term": term, "category": cat, "sources": [], "variants": []}
            if source not in entry["sources"]:
                entry["sources"].append(source)
            if rank[cat] < rank[entry["category"]]:           # keep the most specific category
                entry["category"] = cat
            if variant and variant != entry["term"] and variant not in entry["variants"]:
                entry["variants"].append(variant)

    out = list(merged.values())
    for e in out:
        e["n_sources"] = len([x for x in e["sources"] if x != "input"])
    out.sort(key=lambda e: (rank[e["category"]], -e["n_sources"], e["term"].lower()))
    return out


def lookup_terms(drug, sources=None, min_sources=1, session=None, verbose=False,
                 include_other=False):
    """Detailed lookup. Returns (terms: list[dict], errors: dict)."""
    session = session or make_session()
    per_source, errors = {}, {}
    for src in sources or DEFAULT_SOURCES:
        try:
            pairs = FETCHERS[src](session, drug)
            per_source[src] = pairs
            if verbose:
                print(f"  {src:<8} {len(pairs)} raw names", file=sys.stderr)
        except Exception as e:                                # one bad source must not kill the run
            errors[src] = f"{type(e).__name__}: {e}"
            if verbose:
                print(f"  {src:<8} FAILED ({errors[src]})", file=sys.stderr)
    terms = merge(drug, per_source)
    terms = [t for t in terms if t["category"] == "query" or t["n_sources"] >= min_sources]
    if not include_other:        # 'other' = untyped leftovers (foreign-language INNs, loose synonyms)
        terms = [t for t in terms if t["category"] != "other"]
    return terms, errors




def fetch_search_terms(drug_name, sources=None, min_sources=1, include_other=False,
                       keep_variants=False, verbose=False):
    """
    MAIN ENTRY POINT (importable).

        from fetch_search_terms import fetch_search_terms
        fetch_search_terms("Semaglutide")
        # -> ['Semaglutide', 'Ozempic', 'Rybelsus', 'Wegovy', 'NN9535', ...]

    Returns a list of search-term strings, the drug name first. If every source fails
    (offline / APIs down) it warns on stderr and returns just [drug_name].
    Use lookup_terms() if you need categories, sources and variants per term.
    """
    drug = " ".join(str(drug_name or "").split())
    if not drug:
        raise ValueError("drug_name is required")
    srcs = [x.strip().lower() for x in (sources or DEFAULT_SOURCES)]
    bad = [x for x in srcs if x not in FETCHERS]
    if bad:
        raise ValueError(f"Unknown source(s): {', '.join(bad)}. Choose from: {', '.join(ALL_SOURCES)}")

    terms, errors = lookup_terms(drug, srcs, min_sources, verbose=verbose, include_other=include_other)
    if errors and len(errors) == len(srcs):
        print(f"WARNING: all term sources failed ({'; '.join(f'{k}: {v}' for k, v in errors.items())}). "
              f"Using only '{drug}'.", file=sys.stderr)
        return [drug]
    for src, err in errors.items():
        print(f"warning: {src} failed - {err}", file=sys.stderr)

    out, seen = [], set()
    for t in terms:
        for name in [t["term"]] + (t.get("variants", []) if keep_variants else []):
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def cli():
    ap = argparse.ArgumentParser(prog="fetch_search_terms.py",
                                 description="Fetch drug names/synonyms from public APIs.")
    ap.add_argument("drug", nargs="+", help="Drug name, e.g. Semaglutide")
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help=f"Comma-separated from: {','.join(ALL_SOURCES)} (default: {','.join(DEFAULT_SOURCES)})")
    ap.add_argument("--min-sources", type=int, default=1,
                    help="Keep only terms found in at least N sources (default 1)")
    ap.add_argument("--include-other", action="store_true",
                    help="Also keep untyped names (e.g. foreign-language INNs like 'Semaglutida')")
    ap.add_argument("--keep-variants", action="store_true",
                    help="Also output spelling variants of codes (e.g. NN-9535 alongside NN9535)")
    ap.add_argument("--terms-only", action="store_true",
                    help="Print only a comma-separated list (excluding the input drug)")
    ap.add_argument("--json", metavar="FILE", help="Also save full details to a JSON file")
    args = ap.parse_args()

    drug = " ".join(args.drug).strip()
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in sources if s not in FETCHERS]
    if bad:
        sys.exit(f"Unknown source(s): {', '.join(bad)}. Choose from: {', '.join(ALL_SOURCES)}")

    verbose = not args.terms_only
    if verbose:
        print(f"Fetching search terms for '{drug}' from: {', '.join(sources)}", file=sys.stderr)
    terms, errors = lookup_terms(drug, sources, args.min_sources, verbose=verbose,
                                 include_other=args.include_other)
    extra = []
    for t in terms:
        if t["category"] == "query":
            continue
        extra.append(t["term"])
        if args.keep_variants:
            extra += t.get("variants", [])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"query": drug, "sources": sources, "errors": errors, "terms": terms},
                      fh, indent=2, ensure_ascii=False)

    if errors and len(errors) == len(sources):
        for src, e in errors.items():
            print(f"ERROR: {src} failed - {e}", file=sys.stderr)
        sys.exit("All sources failed (network blocked or APIs down). No terms fetched.")

    if args.terms_only:
        for src, e in errors.items():
            print(f"warning: {src} failed - {e}", file=sys.stderr)
        print(",".join(extra))
        return

    groups = defaultdict(list)
    for t in terms:
        groups[t["category"]].append(t)
    print(f"\n{len(terms)} search terms for '{drug}'\n" + "─" * 60)
    for cat in CATEGORY_ORDER:
        if groups.get(cat):
            print(f"\n{cat.replace('_', ' ').upper()}")
            for t in groups[cat]:
                also = f"  (variants: {', '.join(t['variants'])})" if t.get("variants") else ""
                print(f"  {t['term']:<28} [{', '.join(t['sources'])}]{also}")
    if errors:
        print("\nSources that failed:")
        for s, e in errors.items():
            print(f"  {s}: {e}")
    if extra:
        quoted = ",".join(extra)
        who_name = strip_salt(drug) or drug
        print("\n" + "─" * 60 + "\nwho.py fetches these automatically:\n"
              f'  python who.py "{who_name}"\n'
              "or pass them yourself:\n"
              f'  python who.py "{who_name}" --no-fetch-terms -s "{quoted}"')
    else:
        print("\nNo additional names found. Check spelling, or try --sources chembl,rxnorm,openfda,pubchem")
    if args.json:
        print(f"\nSaved details to {args.json}")


if __name__ == "__main__":
    cli()