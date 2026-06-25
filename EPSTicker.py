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
import os
import sys
import json
import re
import requests
from bs4 import BeautifulSoup
from datetime import date
import time


# ─────────────────────────────────────────────────────────────────────────────
# Parsing logic (unchanged from original)
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
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.ksestocks.com/",
        "Connection": "keep-alive",
    }

    try:
        response = requests.post(url, files=multipart_form_data, headers=headers)
        response.raise_for_status()
    except requests.RequestException as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
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
        company_raw = cols[0].get_text(separator=" ", strip=True)
        date_str = cols[1].get_text(strip=True)
        raw_announcement = cols[2].get_text(separator="\n", strip=True)
        company_name = (
            company_raw.split("(")[0].strip() if "(" in company_raw else company_raw
        )
        rows_data.append((company_name, date_str, raw_announcement))

    return _group_and_parse(symbol, rows_data)


# ─────────────────────────────────────────────────────────────────────────────
# Supabase upsert logic
# ─────────────────────────────────────────────────────────────────────────────

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
    resp = requests.post(url, headers=headers, json=payload)
    if not resp.ok:
        raise RuntimeError(f"Supabase upsert failed [{table}]: {resp.status_code} {resp.text}")
    return resp.json()


def upsert_records(records):
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("SUPABASE_URL environment variable not set")

    headers = get_supabase_headers()

    for rec in records:
        # ── 1. Upsert parent announcement ───────────────────────────────────
        parent_payload = {
            "symbol":            rec["symbol"],
            "company_name":      rec["company_name"],
            "announcement_date": rec["announcement_date"],
            "raw_text":          rec["raw_text"],
        }
        inserted = supabase_upsert(base_url, "announcements", [parent_payload], headers, on_conflict="symbol,announcement_date")
        ann_id = inserted[0]["id"]
        print(f"  ↳ announcement id={ann_id}  {rec['symbol']} {rec['announcement_date']}")

        # ── 2. Upsert financial results ──────────────────────────────────────
        for fr in rec.get("financial_results", []):
            fr_payload = {
                "announcement_id":        ann_id,
                "result_type":            fr["result_type"],
                "result_period":          fr["result_period"],
                "period_ending":          fr["period_ending"],
                "profit_before_tax_mln":  fr["profit_before_tax_mln"],
                "profit_after_tax_mln":   fr["profit_after_tax_mln"],
                "eps":                    fr["eps"],
            }
            supabase_upsert(base_url, "financial_results", [fr_payload], headers, on_conflict="announcement_id,result_type")

        # ── 3. Upsert corporate actions (if any) ────────────────────────────
        ca = rec.get("corporate_actions")
        if ca:
            ca_payload = {
                "announcement_id":   ann_id,
                "dividend":          ca["dividend"],
                "bonus":             ca["bonus"],
                "book_closure_start": ca["book_closure_start"],
                "book_closure_end":   ca["book_closure_end"],
                "agm_date":          ca["agm_date"],
            }
            supabase_upsert(base_url, "corporate_actions", [ca_payload], headers, on_conflict="announcement_id")


# ─────────────────────────────────────────────────────────────────────────────
# Backfill helpers
# ─────────────────────────────────────────────────────────────────────────────

BACKFILL_START = "2018-01-01"


def fetch_symbols_from_db():
    """Pull unique symbols from Supabase via the same RPC your frontend uses."""
    base_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY")
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
    )
    if not resp.ok:
        raise RuntimeError(f"get_unique_symbols RPC failed: {resp.status_code} {resp.text}")

    rows = resp.json()
    symbols = sorted({row["symbol"].strip().upper() for row in rows if row.get("symbol")})
    print(f"  Fetched {len(symbols)} unique symbols from DB")
    return symbols


def run_backfill(upsert=False):
    end_date = date.today().strftime("%Y-%m-%d")
    symbols = fetch_symbols_from_db()
    for symbol in symbols:
        print(f"\n{'─'*50}\nBackfilling {symbol}  {BACKFILL_START} → {end_date}")
        records = scrape_ksestocks_announcements(symbol, BACKFILL_START, end_date)
        print(f"  Found {len(records)} announcement(s)")
        if records and upsert:
            upsert_records(records)
        elif records:
            print(json.dumps(records, indent=2))
            time.sleep(2)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape KSE Financial Announcements → Supabase"
    )
    parser.add_argument("symbol", nargs="?", help="Company symbol (e.g. HCAR)")
    parser.add_argument("start_date", nargs="?", help="Start date YYYY-MM-DD")
    parser.add_argument("end_date", nargs="?", help="End date YYYY-MM-DD")
    parser.add_argument("--upsert", action="store_true",
                        help="Write results to Supabase (requires env vars)")
    parser.add_argument("--backfill", action="store_true",
                        help="Run full backfill for all symbols fetched from DB")

    args = parser.parse_args()

    if args.backfill:
        run_backfill(upsert=args.upsert)
    elif args.symbol and args.start_date and args.end_date:
        records = scrape_ksestocks_announcements(args.symbol, args.start_date, args.end_date)
        if args.upsert and records:
            upsert_records(records)
        else:
            print(json.dumps(records, indent=4))
    else:
        parser.print_help()
        sys.exit(1)