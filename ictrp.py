"""
fetch_who_ictrp_trials.py  v6
─────────────────────────────────────────────────────────────────────────────
Fetches ALL clinical trials from WHO ICTRP (https://trialsearch.who.int)
via the site's own "Export to CSV" feature, instead of scraping the
paginated results table.

WHY v6 CHANGED FROM v5
  v5 scraped the HTML results table page-by-page, driving ASP.NET
  __doPostBack pagination. That approach was unreliable because:
    - The pagination control is "1 2 3 4 5 ... >>" where >> jumps to the
      NEXT GROUP of page-number links, not the next page — so the old
      get_next_btn() logic silently stopped once the last visible group
      had no ">>" control, even with hundreds of pages left unread.
    - Each postback resubmits a growing __VIEWSTATE payload; on large
      result sets this gets slow/unreliable well before the end.
    - A postback that didn't trigger a full navigation event could
      produce a false "stall" (duplicate rows -> early exit).
  ICTRP provides a built-in CSV export that returns the FULL result set
  in one server-side download, bypassing pagination entirely. v6 uses
  that instead: run the search, click "Export to CSV", capture the
  download, done.

INSTALL (run once):
    pip install playwright pandas
    playwright install chromium

USAGE:
    python fetch_who_ictrp_trials.py --query semaglutide
    python fetch_who_ictrp_trials.py --query semaglutide --source CRIS
    python fetch_who_ictrp_trials.py --query semaglutide --show      # visible browser
    python fetch_who_ictrp_trials.py --query semaglutide --debug     # dump HTML + exit
─────────────────────────────────────────────────────────────────────────────
"""

import argparse, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("pip install pandas")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("pip install playwright && playwright install chromium")

try:
    from fetch_search_terms import fetch_search_terms as _fetch_search_terms
except ImportError:
    _fetch_search_terms = None


BASE = "https://trialsearch.who.int"
HOME = f"{BASE}/Default.aspx"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

STEALTH = """
Object.defineProperty(navigator, 'webdriver',  {get: () => undefined});
Object.defineProperty(navigator, 'plugins',    {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages',  {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
"""


# ── Browser setup ──────────────────────────────────────────────────────────
def new_ctx(browser):
    ctx = browser.new_context(
        user_agent=UA,
        locale="en-US",
        viewport={"width": 1366, "height": 900},
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        accept_downloads=True,
    )
    ctx.add_init_script(STEALTH)
    return ctx


def wait_idle(page, extra=0):
    try:
        page.wait_for_load_state("networkidle", timeout=25_000)
    except PWTimeout:
        pass
    if extra:
        time.sleep(extra)


# ── Debug dump ─────────────────────────────────────────────────────────────
def dump(page, fname):
    html = page.content()
    Path(fname).write_text(html, encoding="utf-8")
    print(f"\n[debug] {fname}  ({len(html):,} bytes)  url={page.url}")

    print("\n── INPUTS ──")
    for el in page.query_selector_all("input"):
        print(f"  type={el.get_attribute('type') or '':<10} "
              f"name={el.get_attribute('name') or '':<35} "
              f"id={el.get_attribute('id') or '':<35} "
              f"val={(el.get_attribute('value') or '')[:40]}")

    print("\n── BUTTONS / LINKS mentioning export ──")
    for sel in ["input", "a", "button"]:
        for el in page.query_selector_all(sel):
            txt = (el.inner_text() if sel != "input" else (el.get_attribute("value") or "")) or ""
            if "export" in txt.lower() or "csv" in txt.lower() or "download" in txt.lower():
                print(f"  <{sel}> text/value={txt.strip()[:50]!r} "
                      f"id={el.get_attribute('id')!r} name={el.get_attribute('name')!r}")

    print("\n── BODY TEXT (2000 chars) ──")
    try:
        print(page.inner_text("body")[:2000])
    except Exception:
        pass


def parse_total(page) -> int:
    txt = ""
    try:
        txt = page.inner_text("body")
    except Exception:
        return 0
    for pat in [
        r"(\d[\d,]+)\s+(?:record|result|trial)s?\s+found",
        r"of\s+(\d[\d,]+)\s+(?:record|result|trial)",
        r"Total[:\s]+(\d[\d,]+)",
        r"(\d[\d,]+)\s+trials?\s+match",
        r"1\s*-\s*\d+\s+of\s+(\d[\d,]+)",
    ]:
        m = re.search(pat, txt, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0


# ── Search & submit ────────────────────────────────────────────────────────
def do_search(page, query: str, delay: float, debug: bool) -> bool:
    print(f"  Loading {HOME} …")
    page.goto(HOME, timeout=60_000, wait_until="domcontentloaded")
    wait_idle(page, delay)

    if debug:
        dump(page, "debug_home.html")

    inp = None
    for sel in [
        "input[name*='SearchTerm' i]",
        "input[id*='SearchTerm' i]",
        "input[name*='keyword' i]",
        "input[id*='keyword' i]",
        "input[name*='txtSearch' i]",
        "input[id*='txtSearch' i]",
        "input[type='text']",
    ]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            inp = el
            print(f"  Search box: {sel}")
            break

    if inp is None:
        print("  [ERROR] Search box not found. Run with --show --debug.")
        return False

    inp.click()
    inp.fill("")
    inp.fill(query)
    time.sleep(0.4)
    print(f"  Typed: '{query}'")

    submitted = False
    for sel in [
        "input[type='submit']",
        "button[type='submit']",
        "input[value*='Search' i]",
        "button:has-text('Search')",
        "input[id*='btnSearch' i]",
    ]:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            print(f"  Submit: {sel}")
            try:
                with page.expect_navigation(timeout=45_000, wait_until="domcontentloaded"):
                    btn.click()
            except PWTimeout:
                pass
            submitted = True
            break

    if not submitted:
        print("  No submit button — pressing Enter")
        try:
            with page.expect_navigation(timeout=45_000, wait_until="domcontentloaded"):
                inp.press("Enter")
        except PWTimeout:
            pass

    print("  Waiting for results table …")
    try:
        page.wait_for_function(
            """() => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (/^(NCT|ACTRN|CTRI|ChiCTR|DRKS|IRCT|ISRCTN|jRCT|KCT|PACTR|RPCEC|NTR|RBR)/i.test(a.innerText.trim())) {
                        return true;
                    }
                }
                return false;
            }""",
            timeout=30_000,
        )
        print("  Results table detected.")
    except PWTimeout:
        print("  [warn] Timed out waiting for trial IDs. Proceeding anyway.")

    wait_idle(page, delay)

    if debug:
        dump(page, "debug_results.html")

    return True


# ── Export to CSV (replaces the old pagination scraper) ────────────────────
def export_csv(page, out_path: str, delay: float, debug: bool, timeout_ms: int = 180_000) -> bool:
    """
    Click the site's "Export to CSV" control and save the resulting
    download directly to out_path. This returns the FULL result set in
    one shot, regardless of how many pages the HTML table would have had.

    Some ICTRP result pages require ticking a "select all" checkbox
    before the export button is active/visible — we try that first if
    present, then look for the export control itself.
    """
    # Optional "select all" checkbox some result views require.
    for sel in [
        "input[type='checkbox'][id*='SelectAll' i]",
        "input[type='checkbox'][id*='chkAll' i]",
        "input[type='checkbox'][name*='SelectAll' i]",
    ]:
        cb = page.query_selector(sel)
        if cb and cb.is_visible():
            try:
                cb.check()
                print(f"  Checked 'select all': {sel}")
                time.sleep(0.3)
            except Exception:
                pass
            break

    export_btn = None
    for sel in [
        "input[value*='Export' i]",
        "a:has-text('Export')",
        "button:has-text('Export')",
        "input[id*='Export' i]",
        "input[id*='btnExport' i]",
        "a[id*='Export' i]",
        "a:has-text('CSV')",
        "input[value*='CSV' i]",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                export_btn = loc.first
                print(f"  Export control: {sel}")
                break
        except Exception:
            continue

    if export_btn is None:
        print("  [ERROR] Export button not found. Run with --show --debug to inspect the page.")
        if debug:
            dump(page, "debug_export_missing.html")
        return False

    print("  Clicking export and waiting for download (this can take a while "
          "for large result sets) …")

    # The export button is a regular ASP.NET submit button. Playwright's
    # .click() blocks waiting for the postback navigation to finish, which
    # can take minutes for 1000+ trials and times out. Instead we:
    #   1. Set up expect_download FIRST (proven to catch the CSV on this site)
    #   2. Fire the click via JavaScript so Playwright never waits on navigation
    try:
        with page.expect_download(timeout=timeout_ms) as dl_info:
            # Use JavaScript click — bypasses Playwright's navigation-wait
            page.eval_on_selector(
                "input[value*='Export' i]",
                "el => el.click()"
            )
        download = dl_info.value
    except PWTimeout:
        print("  [ERROR] Timed out waiting for the download to start. "
              "Try --show to watch what happens, or increase --export-timeout.")
        if debug:
            dump(page, "debug_export_after_click.html")
        return False
    except Exception as e:
        print(f"  [ERROR] Export failed: {e}")
        if debug:
            dump(page, "debug_export_after_click.html")
        return False

    download.save_as(out_path)
    print(f"  Download saved → {out_path}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────
def fetch_trials_csv(
    query: str,
    out_path: str,
    delay: float = 3.0,
    headless: bool = True,
    debug: bool = False,
    export_timeout: int = 180_000,
) -> bool:

    print(f"\n{'─'*65}")
    print(f"  WHO ICTRP Fetcher (CSV export)  |  query='{query}'")
    print(f"{'─'*65}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx  = new_ctx(browser)
        page = ctx.new_page()

        ok = do_search(page, query, delay, debug)
        if not ok:
            browser.close()
            return False

        total = parse_total(page)
        print(f"  Total reported on page: {total or '(unknown)'}")

        ok = export_csv(page, out_path, delay, debug, timeout_ms=export_timeout)
        browser.close()
        return ok


def filter_and_summarize(csv_path: str, source_filter: str = ""):
    if not Path(csv_path).exists():
        print("  Nothing to summarize — export did not produce a file.")
        return

    df = pd.read_csv(csv_path, on_bad_lines="warn")
    print(f"\n  ✓ {len(df)} trials in exported CSV.")

    if source_filter:
        # Try to find a registry/source column by name heuristically.
        candidate_cols = [c for c in df.columns if re.search(r"source|registry", c, re.I)]
        if candidate_cols:
            col = candidate_cols[0]
            before = len(df)
            df = df[df[col].astype(str).str.contains(source_filter, case=False, na=False)]
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"  Filter '{source_filter}' on column '{col}': {len(df)}/{before} kept "
                  f"(file overwritten with filtered rows)")
        else:
            print(f"  [warn] Could not find a source/registry column to filter on; "
                  f"columns are: {list(df.columns)}")

    print("\n── Summary ───────────────────────────────────────────────────────")
    for col in df.columns:
        if re.search(r"status|registry|source", col, re.I) and df[col].astype(str).str.strip().any():
            print(f"\nBy {col}:\n{df[col].value_counts().head(15).to_string()}")
    print("──────────────────────────────────────────────────────────────────")


# ── Importable entry point ─────────────────────────────────────────────────

_ALLOWED_REGISTRIES: frozenset[str] = frozenset({
    "anzctr",
    "chictr",
    "clinical trials information system",
    "clinicaltrials.gov",
    "cris",
    "ctri",
    "ctri (india)",        # space variant as it appears in ICTRP exports
    "ctri(india)",         # no-space variant, just in case
    "eu clinical trials register",
    "eu ctis",             # alternate name used in ICTRP for CTIS
    "eudract",
    "jprn",
    "jrct",
    "rebec",
})


def _clean_trial_id(tid: str) -> str:
    """Normalise ICTRP trial IDs to canonical format.

    CTIS2026-527538-29-00  →  2026-527538-29-00   (drop 'CTIS' prefix)
    EUCTR2022-000790-94-SK →  2022-000790-94       (drop 'EUCTR' prefix + last 3 chars)
    """
    u = tid.upper()
    if u.startswith("CTIS"):
        return tid[4:]
    if u.startswith("EUCTR"):
        return tid[5:-3]   # drop 'EUCTR' (5) and country suffix e.g. '-SK' (3)
    return tid


def _safe_name(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w\-]+", "_", s.strip())).strip("_") or "drug"


def _find_col(df: pd.DataFrame, *patterns) -> str | None:
    """Return the first column whose name matches any of the given regex patterns."""
    for pat in patterns:
        for col in df.columns:
            if re.search(pat, col, re.IGNORECASE):
                return col
    return None


def ictrp(
    drug_name: str,
    out_dir: str = "./output",
    fetch_terms: bool = True,
    delay: float = 3.0,
    headless: bool = True,
    export_timeout: int = 180_000,
    workers: int = 6,
) -> pd.DataFrame:
    """
    Main entry point — importable.

        from ictrp import ictrp
        df = ictrp("Semaglutide")   # -> DataFrame with Trial_ID | Registry | Source

    Steps:
      1. Expand drug_name into synonyms via fetch_search_terms (unless fetch_terms=False).
      2. For every search term, trigger the WHO ICTRP "Export to CSV" and download the file.
      3. Parse each CSV, deduplicate on Trial_ID, and return a combined DataFrame.

    The returned DataFrame always has at minimum: Trial_ID, Registry, Source.
    Source is always "ictrp" (the name of this module / file).
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
            print(f"  [ictrp] term lookup failed ({e}) — searching only '{drug}'")
    print(f"  [ictrp] search terms: {terms}")

    os.makedirs(out_dir, exist_ok=True)
    all_dfs: list[pd.DataFrame] = []
    seen_ids: set[str] = set()
    all_xrefs: dict[str, str] = {}
    _NCT_RE = re.compile(r'\bNCT\d{8}\b', re.IGNORECASE)

    # 2. Fetch CSV for each term in parallel
    _lock = threading.Lock()

    def _fetch_term(term: str) -> pd.DataFrame | None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_path = os.path.join(out_dir, f"ictrp_{_safe_name(term)}_{ts}.csv")
        ok = fetch_trials_csv(
            query=term,
            out_path=out_path,
            delay=delay,
            headless=headless,
            export_timeout=export_timeout,
        )
        if not ok or not Path(out_path).exists():
            return None
        try:
            return pd.read_csv(out_path, on_bad_lines="warn")
        except Exception as e:
            print(f"  [ictrp] could not read {out_path}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(terms)),
                            thread_name_prefix="ictrp-term") as pool:
        futures = {pool.submit(_fetch_term, t): t for t in terms}
        for fut in as_completed(futures):
            raw_df = fut.result()
            if raw_df is None:
                continue

            # Map whatever column names WHO ICTRP uses to our standard names
            id_col  = _find_col(raw_df, r"trial.?id", r"^id$", r"trialid")
            reg_col = _find_col(raw_df, r"source.?register", r"registry", r"source", r"register")
            sec_col = _find_col(raw_df, r"secondary.?id", r"other.?id", r"linked")

            if id_col is None:
                print(f"  [ictrp] no Trial ID column found; columns: {list(raw_df.columns)}")
                continue

            rows = []
            with _lock:
                for _, row in raw_df.iterrows():
                    raw_tid = str(row[id_col]).strip()
                    if not raw_tid or raw_tid.lower() == "nan":
                        continue
                    registry = str(row[reg_col]).strip() if reg_col else ""

                    # Keep only recognised registries.
                    # Also drop rows with no registry value: if reg_col was
                    # not found, registry is "" which is not in the allow-set.
                    if registry.lower() not in _ALLOWED_REGISTRIES:
                        continue

                    # Normalise the trial ID (strip CTIS/EUCTR prefixes)
                    tid = _clean_trial_id(raw_tid)
                    if not tid:
                        continue

                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    rows.append({"Trial_ID": tid, "Registry": registry, "Source": "ictrp"})

                    # Extract NCT cross-references from the Secondary IDs column
                    if sec_col:
                        sec_raw = str(row.get(sec_col, ""))
                        for nct_match in _NCT_RE.findall(sec_raw):
                            nct_id = nct_match.upper()
                            if not tid.upper().startswith("NCT"):
                                all_xrefs[tid] = nct_id

            if rows:
                all_dfs.append(pd.DataFrame(rows))

    if not all_dfs:
        print("  [ictrp] no trials found.")
        return pd.DataFrame(columns=["Trial_ID", "Registry", "Source"])

    result = pd.concat(all_dfs, ignore_index=True).drop_duplicates(subset=["Trial_ID"])
    result.attrs["xrefs"] = all_xrefs
    print(f"  [ictrp] {len(result)} unique trials found across {len(terms)} search term(s).")
    return result


# ── CLI ────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--query",  default="semaglutide")
    p.add_argument("--out",    default="")
    p.add_argument("--source", default="",  help="Filter by registry name, e.g. CRIS")
    p.add_argument("--delay",  type=float, default=3)
    p.add_argument("--show",   action="store_true", help="Visible browser")
    p.add_argument("--debug",  action="store_true", help="Dump HTML + exit")
    p.add_argument("--export-timeout", type=int, default=180_000,
                    help="Milliseconds to wait for the CSV download (default 180000)")
    args = p.parse_args()

    out = args.out or f"who_ictrp_{args.query.replace(' ','_')}.csv"

    ok = fetch_trials_csv(
        query          = args.query,
        out_path       = out,
        delay          = args.delay,
        headless       = not args.show,
        debug          = args.debug,
        export_timeout = args.export_timeout,
    )

    if ok:
        filter_and_summarize(out, source_filter=args.source)
    else:
        print("\n  Export failed — see messages above. Try --show --debug to inspect the page "
              "and confirm the export control's selector.")


if __name__ == "__main__":
    main()