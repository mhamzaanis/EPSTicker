"""
kse_scraper.py  –  KSE announcement scraper + Supabase upsert
Usage:
    # print JSON only (original behaviour)
    python kse_scraper.py HCAR 2024-01-01 2024-12-31

    # upsert into Supabase
    python kse_scraper.py HCAR 2024-01-01 2024-12-31 --upsert

    # backfill many symbols from 2018 (used by GitHub Actions)
    python kse_scraper.py --backfill --upsert

Environment variables required for --upsert:
    SUPABASE_URL   – e.g. https://xxxx.supabase.co
    SUPABASE_KEY   – service-role key (not anon key)
"""

import argparse
import logging
import os
import sys
import json
import re
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import time

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing logic (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def parse_announcement_details(text):
    data = {
        "result_period": None, "period_ending": None,
        "unconsolidated_profit_before_tax_mln": None,
        "unconsolidated_profit_after_tax_mln": None,
        "unconsolidated_eps": None,
        "consolidated_profit_before_tax_mln": None,
        "consolidated_profit_after_tax_mln": None,
        "consolidated_eps": None,
        "dividend": None, "bonus": None,
        "book_closure_start": None, "book_closure_end": None,
        "agm_date": None,
    }

    segments = [s.strip() for s in text.split("\n---\n") if s.strip()]

    def _has_explicit_tags(lines):
        return any(("(CONSOLIDATED)" in l or "(UNCONSOLIDATED)" in l) for l in lines)

    def _extract_value(line):
        m = re.search(r"([\d,\.]+)\s*$", line)
        if m:
            val = m.group(1)
            if val.count('.') > 1:
                parts = val.rsplit('.', 1)
                val = parts[0].replace('.', '') + '.' + parts[1]
            return val.replace(",", "")
        m = re.search(r"\(([\d,\.]+)\)\s*$", line)
        if m:
            val = m.group(1)
            if val.count('.') > 1:
                parts = val.rsplit('.', 1)
                val = parts[0].replace('.', '') + '.' + parts[1]
            return "-" + val.replace(",", "")
        return None

    def _apply_line(line, ctx):
        if "PROFIT/LOSS BEFORE TAXATION" in line:
            val = _extract_value(line)
            if val:
                data[f"{ctx}_profit_before_tax_mln"] = val
        elif "PROFIT/LOSS AFTER TAXATION" in line:
            val = _extract_value(line)
            if val:
                data[f"{ctx}_profit_after_tax_mln"] = val
        elif "EPS =" in line:
            raw_eps = line.split("EPS =", 1)[1].strip()
            if raw_eps.startswith("(") and raw_eps.endswith(")"):
                data[f"{ctx}_eps"] = "-" + raw_eps.strip("()")
            else:
                data[f"{ctx}_eps"] = raw_eps

    def _process_segment(lines, forced_context=None):
        current_context = forced_context
        deferred = []
        has_tags = _has_explicit_tags(lines)

        for line in lines:
            line = line.strip()
            if not line or line == "---":
                continue

            period_match = re.search(
                r"FINANCIAL RESULT FOR THE (.*?) ENDED (\d{2}/\d{2}/\d{4})", line
            )
            if period_match:
                if data["result_period"] is None:
                    data["result_period"] = period_match.group(1).strip()
                if data["period_ending"] is None:
                    data["period_ending"] = period_match.group(2).strip()
                continue

            if "DIVIDEND =" in line:
                data["dividend"] = line.split("DIVIDEND =", 1)[1].strip()
                continue
            if re.search(r"BONUS\s*=", line):
                data["bonus"] = line.split("=", 1)[1].strip()
                continue
            if "BOOK CLOSURE FROM" in line:
                rest = line.replace("BOOK CLOSURE FROM", "").strip()
                parts = re.findall(r"\d{2}/\d{2}/\d{4}", rest)
                if parts:
                    data["book_closure_start"] = parts[0]
                    if len(parts) > 1:
                        data["book_closure_end"] = parts[1]
                continue
            if "BOOK CLOSURE TO" in line:
                m = re.search(r"(\d{2}/\d{2}/\d{4})", line)
                if m:
                    data["book_closure_end"] = m.group(1)
                continue
            if "ANNUAL GENERAL MEETING" in line:
                m = re.search(r"(\d{2}/\d{2}/\d{4})", line)
                if m:
                    data["agm_date"] = m.group(1)
                continue

            if "(CONSOLIDATED)" in line:
                current_context = "consolidated"
                _apply_line(line, current_context)
                continue
            if "(UNCONSOLIDATED)" in line:
                current_context = "unconsolidated"
                _apply_line(line, current_context)
                continue

            is_financial = any(
                kw in line
                for kw in ("PROFIT/LOSS BEFORE TAXATION", "PROFIT/LOSS AFTER TAXATION", "EPS =")
            )
            if is_financial:
                if current_context is not None:
                    _apply_line(line, current_context)
                else:
                    deferred.append(line)

        if deferred:
            if forced_context:
                fallback = forced_context
            elif not has_tags:
                fallback = "unconsolidated"
            else:
                fallback = current_context or "unconsolidated"
            for line in deferred:
                _apply_line(line, fallback)

    if len(segments) == 2:
        seg0_lines = segments[0].split("\n")
        seg1_lines = segments[1].split("\n")
        if not _has_explicit_tags(seg0_lines) and not _has_explicit_tags(seg1_lines):
            _process_segment(seg0_lines, forced_context="unconsolidated")
            _process_segment(seg1_lines, forced_context="consolidated")
        else:
            _process_segment(seg0_lines)
            _process_segment(seg1_lines)
    else:
        _process_segment(text.split("\n"))

    return data


def _group_and_parse(symbol, rows_data):
    from collections import OrderedDict
    from datetime import datetime

    def clean_db_date(date_str):
        if not date_str:
            return None
        cleaned = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
        try:
            return datetime.strptime(cleaned, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def clean_period_date(date_str):
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    groups = OrderedDict()
    for company_name, date_str, raw in rows_data:
        key = (symbol.upper(), date_str)
        if key not in groups:
            groups[key] = {"company_name": company_name, "raws": [raw]}
        else:
            groups[key]["raws"].append(raw)

    results = []
    for (sym, date_str), grp in groups.items():
        combined_raw = "\n---\n".join(grp["raws"])
        flat_data = parse_announcement_details(combined_raw)

        iso_announcement_date = clean_db_date(date_str)
        iso_period_ending = clean_period_date(flat_data["period_ending"])

        parent_record = {
            "symbol": sym,
            "company_name": grp["company_name"],
            "announcement_date": iso_announcement_date,
            "raw_text": combined_raw,
            "financial_results": [],
            "corporate_actions": None,
        }

        if any([flat_data["unconsolidated_profit_before_tax_mln"],
                flat_data["unconsolidated_profit_after_tax_mln"],
                flat_data["unconsolidated_eps"]]):
            parent_record["financial_results"].append({
                "result_type": "UNCONSOLIDATED",
                "result_period": flat_data["result_period"],
                "period_ending": iso_period_ending,
                "profit_before_tax_mln": flat_data["unconsolidated_profit_before_tax_mln"],
                "profit_after_tax_mln": flat_data["unconsolidated_profit_after_tax_mln"],
                "eps": flat_data["unconsolidated_eps"],
            })

        if any([flat_data["consolidated_profit_before_tax_mln"],
                flat_data["consolidated_profit_after_tax_mln"],
                flat_data["consolidated_eps"]]):
            parent_record["financial_results"].append({
                "result_type": "CONSOLIDATED",
                "result_period": flat_data["result_period"],
                "period_ending": iso_period_ending,
                "profit_before_tax_mln": flat_data["consolidated_profit_before_tax_mln"],
                "profit_after_tax_mln": flat_data["consolidated_profit_after_tax_mln"],
                "eps": flat_data["consolidated_eps"],
            })

        if any([flat_data["dividend"], flat_data["bonus"],
                flat_data["book_closure_start"], flat_data["book_closure_end"],
                flat_data["agm_date"]]):
            parent_record["corporate_actions"] = {
                "dividend": flat_data["dividend"],
                "bonus": flat_data["bonus"],
                "book_closure_start": clean_period_date(flat_data["book_closure_start"]),
                "book_closure_end": clean_period_date(flat_data["book_closure_end"]),
                "agm_date": clean_period_date(flat_data["agm_date"]),
            }

        results.append(parent_record)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Scraping
# ─────────────────────────────────────────────────────────────────────────────

SCRAPE_TIMEOUT = 30   # seconds per request
SCRAPE_RETRIES = 3    # attempts before giving up
SCRAPE_BACKOFF = 5    # seconds between retries

# One session per process — keeps TCP connections alive across all symbol requests.
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.ksestocks.com/",
    "Connection": "keep-alive",
})


def scrape_ksestocks_announcements(symbol, start_date, end_date):
    url = "https://www.ksestocks.com/Announcements"
    multipart_form_data = {
        "dtype": (None, "byscrip"),
        "sdate": (None, end_date),
        "ssym": (None, symbol.upper()),
        "rfdate": (None, start_date),
        "rtdate": (None, end_date),
        "mansear": (None, ""),
    }

    response = None
    for attempt in range(1, SCRAPE_RETRIES + 1):
        try:
            response = _session.post(url, files=multipart_form_data, timeout=SCRAPE_TIMEOUT)
            response.raise_for_status()
            break
        except requests.RequestException as e:
            log.warning("[attempt %d/%d] %s scrape error: %s", attempt, SCRAPE_RETRIES, symbol, e)
            if attempt < SCRAPE_RETRIES:
                time.sleep(SCRAPE_BACKOFF * attempt)   # 5s, 10s, …
            else:
                return []

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.find_all("tr", class_="data-tr")
    if not rows:
        return []

    rows_data = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) != 3:
            continue
        for br in cols[2].find_all("br"):
            br.replace_with("\n")
        company_raw     = cols[0].get_text(separator=" ", strip=True)
        date_str        = cols[1].get_text(strip=True)
        raw_announcement = cols[2].get_text(separator="\n", strip=True)
        company_name    = company_raw.split("(")[0].strip() if "(" in company_raw else company_raw
        rows_data.append((company_name, date_str, raw_announcement))

    return _group_and_parse(symbol, rows_data)


# ─────────────────────────────────────────────────────────────────────────────
# Supabase helpers
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_TIMEOUT = 30   # seconds per Supabase REST call


def get_supabase_headers():
    key = os.environ.get("SUPABASE_KEY")
    if not key:
        raise RuntimeError("SUPABASE_KEY environment variable not set")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def supabase_upsert(base_url, table, payload, headers, on_conflict=None):
    """POST to Supabase REST API with ON CONFLICT DO UPDATE semantics."""
    url = f"{base_url}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    resp = requests.post(url, headers=headers, json=payload, timeout=SUPABASE_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Supabase upsert failed [{table}]: {resp.status_code} {resp.text}")
    return resp.json()


def fetch_existing_announcements(start_date):
    """
    Return a dict { (symbol, announcement_date): announcement_id }
    for all announcements in the DB on or after start_date.

    This serves two purposes:
      1. Dedup filter  — skip parent inserts for rows already in the DB.
      2. ID lookup     — supply announcement_id for child upserts without
                         needing to re-insert the parent.

    ON CONFLICT in Supabase remains the final safety net for race conditions.
    """
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY")
    if not base_url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")

    url = (
        f"{base_url}/rest/v1/announcements"
        f"?select=id,symbol,announcement_date"
        f"&announcement_date=gte.{start_date}"
    )
    resp = requests.get(
        url,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=SUPABASE_TIMEOUT,
    )
    if not resp.ok:
        log.warning("Could not fetch existing announcements (%s), skipping dedup", resp.status_code)
        return {}

    return {
        (row["symbol"], row["announcement_date"]): row["id"]
        for row in resp.json()
    }


def _to_numeric(val):
    """Convert parsed string values to float, or None if empty/unparseable.
    Prevents Postgres 22P02 'invalid input syntax for type numeric' errors."""
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def upsert_records(records, existing, stats):
    """
    Sync one symbol's announcements to Supabase.

    Flow per record:
      - Parent already in DB  → reuse cached ID, skip insert, still upsert children.
      - Parent is new         → bulk insert, store returned ID in cache, upsert children.

    existing: dict { (symbol, announcement_date): id } — mutated in place as
              new parents are inserted, so the cache stays live across the full run.
    stats:    dict of run-wide counters, mutated in place.

    ON CONFLICT clauses remain on every table as the DB-level safety net.
    """
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("SUPABASE_URL environment variable not set")

    headers = get_supabase_headers()

    # ── Partition records into new vs already-known ───────────────────────────
    new_records      = []
    existing_records = []
    for rec in records:
        key = (rec["symbol"], rec["announcement_date"])
        if key in existing:
            existing_records.append(rec)
            stats["reused"] += 1
        else:
            new_records.append(rec)

    # ── 1. Bulk insert new parent announcements ───────────────────────────────
    id_map = {}   # (symbol, date) → id, populated below

    if new_records:
        parent_payloads = [
            {
                "symbol":            rec["symbol"],
                "company_name":      rec["company_name"],
                "announcement_date": rec["announcement_date"],
                "raw_text":          rec["raw_text"],
            }
            for rec in new_records
        ]
        inserted = supabase_upsert(
            base_url, "announcements", parent_payloads, headers,
            on_conflict="symbol,announcement_date"
        )
        for row in inserted:
            key = (row["symbol"], row["announcement_date"])
            id_map[key]    = row["id"]
            existing[key]  = row["id"]   # ← live cache update
            stats["inserted"] += 1
            log.info("  ↳ NEW  id=%-6s  %s  %s", row["id"], row["symbol"], row["announcement_date"])
    else:
        log.info("  No new parent announcements — all already in DB.")

    # Log reused IDs (pulled from cache)
    for rec in existing_records:
        key = (rec["symbol"], rec["announcement_date"])
        id_map[key] = existing[key]
        log.info("  ↳ SKIP id=%-6s  %s  %s (already exists)", existing[key], rec["symbol"], rec["announcement_date"])

    # ── 2. Bulk upsert financial results (new AND existing parents) ───────────
    fr_payloads = []
    for rec in records:
        ann_id = id_map.get((rec["symbol"], rec["announcement_date"]))
        if ann_id is None:
            continue
        for fr in rec.get("financial_results", []):
            fr_payloads.append({
                "announcement_id":       ann_id,
                "result_type":           fr["result_type"],
                "result_period":         fr["result_period"],
                "period_ending":         fr["period_ending"],
                "profit_before_tax_mln": _to_numeric(fr["profit_before_tax_mln"]),
                "profit_after_tax_mln":  _to_numeric(fr["profit_after_tax_mln"]),
                "eps":                   _to_numeric(fr["eps"]),
            })
    if fr_payloads:
        supabase_upsert(
            base_url, "financial_results", fr_payloads, headers,
            on_conflict="announcement_id,result_type"
        )
        stats["fr_rows"] += len(fr_payloads)

    # ── 3. Bulk upsert corporate actions (new AND existing parents) ───────────
    ca_payloads = []
    for rec in records:
        ann_id = id_map.get((rec["symbol"], rec["announcement_date"]))
        if ann_id is None or not rec.get("corporate_actions"):
            continue
        ca = rec["corporate_actions"]
        ca_payloads.append({
            "announcement_id":    ann_id,
            "dividend":           ca["dividend"],
            "bonus":              ca["bonus"],
            "book_closure_start": ca["book_closure_start"],
            "book_closure_end":   ca["book_closure_end"],
            "agm_date":           ca["agm_date"],
        })
    if ca_payloads:
        supabase_upsert(
            base_url, "corporate_actions", ca_payloads, headers,
            on_conflict="announcement_id"
        )
        stats["ca_rows"] += len(ca_payloads)


# ─────────────────────────────────────────────────────────────────────────────
# Backfill / daily run
# ─────────────────────────────────────────────────────────────────────────────

BACKFILL_START       = "2018-01-01"
DAILY_LOOKBACK_DAYS  = 14   # wide enough to catch late-filed announcements


def fetch_symbols_from_db():
    """Pull unique symbols from Supabase via the same RPC the frontend uses."""
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key      = os.environ.get("SUPABASE_KEY")
    if not base_url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")

    resp = requests.post(
        f"{base_url}/rest/v1/rpc/get_unique_symbols",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={},
        timeout=SUPABASE_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"get_unique_symbols RPC failed: {resp.status_code} {resp.text}")

    rows    = resp.json()
    symbols = sorted({row["symbol"].strip().upper() for row in rows if row.get("symbol")})
    log.info("Fetched %d unique symbols from DB", len(symbols))
    return symbols


def run_backfill(upsert=False, is_daily=False):
    run_start  = time.monotonic()
    end_date   = date.today().strftime("%Y-%m-%d")
    start_date = (
        (date.today() - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        if is_daily else BACKFILL_START
    )
    mode = f"daily ({DAILY_LOOKBACK_DAYS}-day window)" if is_daily else "full backfill"
    log.info("Mode: %s  |  Window: %s → %s", mode, start_date, end_date)

    symbols = fetch_symbols_from_db()

    # Single upfront fetch → { (symbol, date): id }
    # Mutated live as new parents are inserted during the run.
    existing = fetch_existing_announcements(start_date) if upsert else {}
    log.info("%d existing announcement(s) in DB for this window", len(existing))

    # Run-wide counters
    stats = {
        "scraped":  0,   # total announcement records scraped
        "inserted": 0,   # new parent rows inserted
        "reused":   0,   # parents skipped (ID reused from cache)
        "fr_rows":  0,   # financial_result rows upserted
        "ca_rows":  0,   # corporate_action rows upserted
    }
    errors = []

    for symbol in symbols:
        log.info("─" * 50)
        log.info("Fetching %s  %s → %s", symbol, start_date, end_date)
        try:
            records = scrape_ksestocks_announcements(symbol, start_date, end_date)
            if records:
                stats["scraped"] += len(records)
                log.info("Found %d announcement(s)", len(records))
                if upsert:
                    upsert_records(records, existing, stats)
                else:
                    log.info("Dry run – no upsert performed")
            else:
                log.info("No announcements found in this period.")
        except Exception as e:
            log.error("✗ ERROR for %s: %s", symbol, e)
            errors.append((symbol, str(e)))
        time.sleep(2)   # be polite to ksestocks

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed     = time.monotonic() - run_start
    total_syms  = len(symbols)
    failed_syms = len(errors)
    ok_syms     = total_syms - failed_syms

    log.info("═" * 50)
    log.info("Run complete in %.0fs", elapsed)
    log.info("  Symbols          : %d processed, %d failed", ok_syms, failed_syms)
    log.info("  Announcements    : %d scraped", stats["scraped"])
    log.info("  Parent rows      : %d inserted, %d reused (skipped)", stats["inserted"], stats["reused"])
    log.info("  Financial results: %d rows upserted", stats["fr_rows"])
    log.info("  Corporate actions: %d rows upserted", stats["ca_rows"])

    if errors:
        log.error("%d symbol(s) failed:", failed_syms)
        for sym, err in errors:
            log.error("  %s: %s", sym, err)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape KSE Financial Announcements → Supabase"
    )
    parser.add_argument("symbol",     nargs="?", help="Company symbol (e.g. HCAR)")
    parser.add_argument("start_date", nargs="?", help="Start date YYYY-MM-DD")
    parser.add_argument("end_date",   nargs="?", help="End date YYYY-MM-DD")
    parser.add_argument("--upsert",   action="store_true",
                        help="Write results to Supabase (requires env vars)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full backfill from 2018 for all symbols in DB")
    parser.add_argument("--daily",    action="store_true",
                        help=f"Incremental sync (last {DAILY_LOOKBACK_DAYS} days) for all symbols")

    args = parser.parse_args()

    if args.backfill:
        run_backfill(upsert=args.upsert, is_daily=False)
    elif args.daily:
        run_backfill(upsert=args.upsert, is_daily=True)
    elif args.symbol and args.start_date and args.end_date:
        records = scrape_ksestocks_announcements(args.symbol, args.start_date, args.end_date)
        if args.upsert and records:
            # Single-symbol path: build a minimal existing cache from DB
            existing = fetch_existing_announcements(args.start_date)
            stats    = {"scraped": 0, "inserted": 0, "reused": 0, "fr_rows": 0, "ca_rows": 0}
            upsert_records(records, existing, stats)
        else:
            print(json.dumps(records, indent=4))
    else:
        parser.print_help()
        sys.exit(1)