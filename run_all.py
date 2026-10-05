#!/usr/bin/env python3
"""
run_all.py - Orchestrator: run NICE, PubMed, and WHO ICTRP (via ictrp.py) for a drug
and produce a single combined output: Trial_ID | Registry | Source

USAGE
    python run_all.py                        # defaults to Semaglutide
    python run_all.py Tirzepatide
    python run_all.py Semaglutide --out results
    python run_all.py Semaglutide --skip nice pubmed
    python run_all.py Semaglutide --only ictrp

Each source uses fetch_search_terms internally to expand the drug name into all
known synonyms/brand names before searching. The final output is a deduplicated
table of unique (Trial_ID, Source) pairs saved as a CSV.
"""

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
try:
    import pandas as pd
except ImportError:
    sys.exit("pip install pandas")


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_name(s: str) -> str:
    import re
    return re.sub(r"_+", "_", re.sub(r"[^\w\-]+", "_", s.strip())).strip("_") or "drug"


def banner(title: str) -> None:
    line = "═" * 70
    print(f"\n{line}\n  {title}\n{line}")


def section(title: str) -> None:
    print(f"\n{'─' * 70}\n  ▶  {title}\n{'─' * 70}")


# ── source runners ────────────────────────────────────────────────────────────

def run_nice(drug: str, out_dir: str) -> pd.DataFrame:
    from nice import nice  # type: ignore[import]
    section("NICE Technology Appraisals")
    df = nice(drug, verbose=True)
    if not isinstance(df, pd.DataFrame):
        # nice() returned a list of IDs (pandas unavailable on that side)
        df = pd.DataFrame({"Trial_ID": df, "Registry": "", "Source": "nice"})
    print(f"  NICE: {len(df)} trial ID(s)")
    return df


def run_pubmed(drug: str, out_dir: str) -> pd.DataFrame:
    from pubmed import pubmed  # type: ignore[import]
    section("PubMed")
    df = pubmed(drug, outdir=out_dir)
    print(f"  PubMed: {len(df)} unique trial ID(s)")
    return df


def run_ictrp(drug: str, out_dir: str) -> pd.DataFrame:
    from ictrp import ictrp  # type: ignore[import]
    section("WHO ICTRP (CSV export)")
    df = ictrp(drug, out_dir=out_dir)
    print(f"  ICTRP: {len(df)} unique trial ID(s)")
    return df


def run_eu(drug: str, out_dir: str) -> pd.DataFrame:
    from eu import eu  # type: ignore[import]
    section("EU Registers (CTIS + EudraCT)")
    df = eu(drug)
    print(f"  EU: {len(df)} unique trial ID(s)")
    return df


def run_ct(drug: str, out_dir: str) -> pd.DataFrame:
    from ct import ct  # type: ignore[import]
    section("ClinicalTrials.gov")
    df = ct(drug)
    print(f"  ClinicalTrials.gov: {len(df)} unique trial ID(s)")
    return df


# ── Cross-registry deduplication ─────────────────────────────────────────────

class _UF:
    """Union-Find that prefers NCT IDs as cluster representatives."""

    def __init__(self) -> None:
        self._p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._p.setdefault(x, x)
        if self._p[x] != x:
            self._p[x] = self.find(self._p[x])
        return self._p[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Always promote the NCT ID to be the root
        if rb.upper().startswith("NCT"):
            self._p[ra] = rb
        else:
            self._p[rb] = ra


def _canonicalize(combined: pd.DataFrame, xrefs: dict[str, str]) -> pd.DataFrame:
    """
    Collapse rows where different Trial_IDs refer to the same trial.
    xrefs: {other_id: nct_id} pairs collected from source metadata.
    When two IDs merge, the NCT ID is kept; Source values are joined.
    """
    if not xrefs:
        return combined

    uf = _UF()
    for other_id, nct_id in xrefs.items():
        uf.union(other_id, nct_id)

    combined = combined.copy()
    combined["Trial_ID"] = combined["Trial_ID"].map(lambda tid: uf.find(tid))

    return (
        combined.groupby("Trial_ID", sort=False)
        .agg(
            Registry=("Registry", lambda s: next((v for v in s if v), "")),
            Source=("Source", lambda s: ", ".join(dict.fromkeys(v for v in s if v))),
        )
        .reset_index()
    )[["Trial_ID", "Registry", "Source"]]


# ── main ──────────────────────────────────────────────────────────────────────

RUNNERS = {
    "nice":   run_nice,
    "pubmed": run_pubmed,
    "ictrp":  run_ictrp,
    "eu":     run_eu,
    "ct":     run_ct,
}

_EXCLUDED_REGISTRIES: frozenset[str] = frozenset({
    "bioproject", "geo", "figshare", "isrctn", "pdb", "umin-ctr",
})


def run_all(drug: str, out_dir: str = "output", sources: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Run all (or selected) sources for *drug* sequentially and return a single
    deduplicated DataFrame with columns: Trial_ID | Registry | Source.
    Each source parallelises its own synonym searches internally (6 workers).

        from run_all import run_all
        df, summary = run_all("Semaglutide")
    """
    to_run = sources or list(RUNNERS.keys())
    os.makedirs(out_dir, exist_ok=True)

    pieces: list[pd.DataFrame] = []
    all_xrefs: dict[str, str] = {}
    summary: dict[str, dict] = {}

    for source in to_run:
        t1 = time.time()
        try:
            df = RUNNERS[source](drug, out_dir)
            for col in ("Trial_ID", "Registry", "Source"):
                if col not in df.columns:
                    df[col] = "" if col != "Source" else source
            pieces.append(df[["Trial_ID", "Registry", "Source"]])
            all_xrefs.update(df.attrs.get("xrefs", {}))
            summary[source] = {"status": "ok", "count": len(df), "elapsed": time.time() - t1}
        except Exception:
            tb = traceback.format_exc()
            summary[source] = {"status": "error", "error": tb, "count": 0, "elapsed": time.time() - t1}
            print(f"\n  [ERROR] {source} failed:\n{tb}", file=sys.stderr)

    # Combine: one row per Trial_ID; if found in multiple sources, join them
    if pieces:
        raw = pd.concat(pieces, ignore_index=True)
        raw = raw[raw["Trial_ID"].str.strip() != ""]
        combined = (
            raw.groupby("Trial_ID", sort=False)
            .agg(
                Registry=("Registry", lambda s: next((v for v in s if v), "")),
                Source=("Source", lambda s: ", ".join(dict.fromkeys(v for v in s if v))),
            )
            .reset_index()
        )[["Trial_ID", "Registry", "Source"]]

        # Cross-registry deduplication: merge rows where different IDs refer
        # to the same trial (secondary ID cross-references), keeping the NCT ID.
        if all_xrefs:
            before = len(combined)
            combined = _canonicalize(combined, all_xrefs)
            merged = before - len(combined)
            if merged:
                print(f"\n  [dedup] cross-registry merge: {merged} row(s) collapsed via secondary IDs.")

        # Drop rows whose registry is in the exclusion list
        mask = combined["Registry"].str.lower().isin(_EXCLUDED_REGISTRIES)
        if mask.any():
            print(f"\n  [filter] dropped {mask.sum()} row(s) from excluded registries "
                  f"({', '.join(sorted(combined.loc[mask, 'Registry'].unique()))}).")
            combined = combined[~mask].reset_index(drop=True)
    else:
        combined = pd.DataFrame(columns=["Trial_ID", "Registry", "Source"])

    return combined, summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run NICE, PubMed, and WHO ICTRP fetchers and save a combined Trial_ID | Registry | Source output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("drug", nargs="?", default="Semaglutide",
                    help="Drug / molecule name (default: Semaglutide)")
    ap.add_argument("--out", default="output",
                    help="Output directory (default: output)")
    ap.add_argument("--skip", nargs="+", choices=list(RUNNERS),
                    default=[], metavar="SOURCE",
                    help="Sources to skip: nice pubmed ictrp eu ct")
    ap.add_argument("--only", nargs="+", choices=list(RUNNERS),
                    default=[], metavar="SOURCE",
                    help="Run only these sources (overrides --skip)")
    args = ap.parse_args()

    drug = args.drug.strip()
    out_dir = args.out

    to_run = list(RUNNERS.keys())
    if args.only:
        to_run = [s for s in to_run if s in args.only]
    elif args.skip:
        to_run = [s for s in to_run if s not in args.skip]

    banner(f"Clinical Evidence Orchestrator  ·  drug: {drug}")
    print(f"  Sources : {', '.join(to_run)}")
    print(f"  Out dir : {os.path.abspath(out_dir)}")
    print(f"  Started : {datetime.now():%Y-%m-%d %H:%M:%S}")

    t0 = time.time()
    combined, summary = run_all(drug, out_dir, to_run)

    # ── save combined output ──────────────────────────────────────────────────
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"trials_{_safe_name(drug)}_{ts}.csv")
    combined.to_csv(out_path, index=False, encoding="utf-8-sig")

    # ── print summary ─────────────────────────────────────────────────────────
    banner("SUMMARY")
    any_error = False
    for source, info in summary.items():
        icon = "✓" if info["status"] == "ok" else "✗"
        print(f"\n  {icon}  {source.upper()}  ({info['elapsed']:.1f}s)  —  {info['count']} trial ID(s)")
        if info["status"] == "error":
            any_error = True
            print(f"     {info['error'].strip().splitlines()[-1]}")

    print(f"\n  Combined unique (Trial_ID, Source) rows : {len(combined)}")
    print(f"  Unique Trial IDs across all sources     : {combined['Trial_ID'].nunique()}")
    print(f"  Output file : {os.path.abspath(out_path)}")
    print(f"  Total elapsed : {time.time() - t0:.1f}s")
    print(f"  Finished : {datetime.now():%Y-%m-%d %H:%M:%S}")

    # ── print the table ───────────────────────────────────────────────────────
    if not combined.empty:
        banner("RESULTS (Trial_ID | Registry | Source)")
        try:
            with pd.option_context("display.max_rows", 200, "display.max_colwidth", 40):
                print(combined.to_string(index=False))
        except Exception:
            print(combined.to_csv(index=False))

    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
