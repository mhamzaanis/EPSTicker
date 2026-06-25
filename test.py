import argparse
import requests
from bs4 import BeautifulSoup
import sys
import json
import re


def parse_announcement_details(text):
    """
    Two-pass parser.
    Special case: when the merged raw_text contains a '---' separator AND neither
    block has explicit (UNCONSOLIDATED)/(CONSOLIDATED) tags, treat the first block
    as unconsolidated and the second as consolidated.
    """
    data = {
        "result_period": None,
        "period_ending": None,
        "unconsolidated_profit_before_tax_mln": None,
        "unconsolidated_profit_after_tax_mln": None,
        "unconsolidated_eps": None,
        "consolidated_profit_before_tax_mln": None,
        "consolidated_profit_after_tax_mln": None,
        "consolidated_eps": None,
        "dividend": None,
        "bonus": None,
        "book_closure_start": None,
        "book_closure_end": None,
        "agm_date": None,
    }

    # ── Split on '---' separator inserted by _merge_records ──────────────────
    # If the text was assembled from two separate HTML rows, they are joined with
    # '\n---\n'.  Process each segment independently so we can assign the correct
    # context to tag-free blocks.
    segments = [s.strip() for s in text.split("\n---\n") if s.strip()]

    def _has_explicit_tags(lines):
        return any(("(CONSOLIDATED)" in l or "(UNCONSOLIDATED)" in l) for l in lines)

    def _extract_value(line):
        # Capture any combination of digits, commas, and periods at the end of the line
        m = re.search(r"([\d,\.]+)\s*$", line)
        if m:
            val = m.group(1)
            # If there are multiple periods (a PSX typo), keep only the last one as the decimal
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
        """
        Parse one segment of lines.
        forced_context: if set, treat ALL tag-free financial lines as this context.
        """
        current_context = forced_context
        deferred = []
        has_tags = _has_explicit_tags(lines)

        for line in lines:
            line = line.strip()
            if not line or line == "---":
                continue

            # Period header
            period_match = re.search(
                r"FINANCIAL RESULT FOR THE (.*?) ENDED (\d{2}/\d{2}/\d{4})", line
            )
            if period_match:
                # Only set once (first occurrence wins across segments)
                if data["result_period"] is None:
                    data["result_period"] = period_match.group(1).strip()
                if data["period_ending"] is None:
                    data["period_ending"] = period_match.group(2).strip()
                continue

            # Corporate actions
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

            # Explicit context tags
            if "(CONSOLIDATED)" in line:
                current_context = "consolidated"
                _apply_line(line, current_context)
                continue
            if "(UNCONSOLIDATED)" in line:
                current_context = "unconsolidated"
                _apply_line(line, current_context)
                continue

            # Financial lines without a tag
            is_financial = any(
                kw in line
                for kw in ("PROFIT/LOSS BEFORE TAXATION", "PROFIT/LOSS AFTER TAXATION", "EPS =")
            )
            if is_financial:
                if current_context is not None:
                    _apply_line(line, current_context)
                else:
                    deferred.append(line)

        # Resolve deferred lines
        if deferred:
            if forced_context:
                fallback = forced_context
            elif not has_tags:
                fallback = "unconsolidated"
            else:
                fallback = current_context or "unconsolidated"
            for line in deferred:
                _apply_line(line, fallback)

    # ── Determine forced contexts for each segment ───────────────────────────
    if len(segments) == 2:
        seg0_lines = segments[0].split("\n")
        seg1_lines = segments[1].split("\n")
        seg0_has_tags = _has_explicit_tags(seg0_lines)
        seg1_has_tags = _has_explicit_tags(seg1_lines)

        if not seg0_has_tags and not seg1_has_tags:
            # Two tag-free blocks → first is unconsolidated, second is consolidated
            _process_segment(seg0_lines, forced_context="unconsolidated")
            _process_segment(seg1_lines, forced_context="consolidated")
        else:
            # At least one block has tags — process normally
            _process_segment(seg0_lines)
            _process_segment(seg1_lines)
    else:
        # Single segment (normal case)
        all_lines = text.split("\n")
        _process_segment(all_lines)

    return data


def _group_and_parse(symbol, rows_data):
    """
    Groups raw scraped lines and normalizes them into separate structures 
    matching a 3-table relational relational DB schema layout.
    """
    from collections import OrderedDict
    from datetime import datetime

    def clean_db_date(date_str):
        """Converts human readable 'April 22nd, 2019' to standard ISO '2019-04-22' for DB."""
        if not date_str:
            return None
        # Clean up ordinals (st, nd, rd, th)
        cleaned = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
        try:
            return datetime.strptime(cleaned, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def clean_period_date(date_str):
        """Converts standard '31/03/2019' string to database '2019-03-31' layout."""
        if not date_str:
            return None
        try:
            # Fixed the typo here: "%d/%m/%Y" instead of "%d/%m/%m"
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

        # ── ENTITY 1: Parent Announcement Entry ──────────────────────────────
        parent_record = {
            "symbol": sym,
            "company_name": grp["company_name"],
            "announcement_date": iso_announcement_date,
            "raw_text": combined_raw,
            "financial_results": [],
            "corporate_actions": None
        }

        # ── ENTITY 2: Child Financial Results (Pivoted Rows) ─────────────────
        # Check if unconsolidated metrics exist
        if any([flat_data["unconsolidated_profit_before_tax_mln"], 
                flat_data["unconsolidated_profit_after_tax_mln"], 
                flat_data["unconsolidated_eps"]]):
            parent_record["financial_results"].append({
                "result_type": "UNCONSOLIDATED",
                "result_period": flat_data["result_period"],
                "period_ending": iso_period_ending,
                "profit_before_tax_mln": flat_data["unconsolidated_profit_before_tax_mln"],
                "profit_after_tax_mln": flat_data["unconsolidated_profit_after_tax_mln"],
                "eps": flat_data["unconsolidated_eps"]
            })

        # Check if consolidated metrics exist
        if any([flat_data["consolidated_profit_before_tax_mln"], 
                flat_data["consolidated_profit_after_tax_mln"], 
                flat_data["consolidated_eps"]]):
            parent_record["financial_results"].append({
                "result_type": "CONSOLIDATED",
                "result_period": flat_data["result_period"],
                "period_ending": iso_period_ending,
                "profit_before_tax_mln": flat_data["consolidated_profit_before_tax_mln"],
                "profit_after_tax_mln": flat_data["consolidated_profit_after_tax_mln"],
                "eps": flat_data["consolidated_eps"]
            })

        # ── ENTITY 3: Child Corporate Actions ─────────────────────────────────
        if any([flat_data["dividend"], flat_data["bonus"], 
                flat_data["book_closure_start"], flat_data["book_closure_end"], 
                flat_data["agm_date"]]):
            parent_record["corporate_actions"] = {
                "dividend": flat_data["dividend"],
                "bonus": flat_data["bonus"],
                "book_closure_start": clean_period_date(flat_data["book_closure_start"]),
                "book_closure_end": clean_period_date(flat_data["book_closure_end"]),
                "agm_date": clean_period_date(flat_data["agm_date"])
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

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.post(url, files=multipart_form_data, headers=headers)
        response.raise_for_status()
    except requests.RequestException as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.find_all("tr", class_="data-tr")

    if not rows:
        print(json.dumps([]))
        return

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

    results = _group_and_parse(symbol, rows_data)
    print(json.dumps(results, indent=4))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape KSE Financial Announcements to Tabular JSON"
    )
    parser.add_argument("symbol", help="Company symbol (e.g., HCAR, ILP)")
    parser.add_argument("start_date", help="Start date in YYYY-MM-DD format")
    parser.add_argument("end_date", help="End date in YYYY-MM-DD format")

    args = parser.parse_args()
    scrape_ksestocks_announcements(args.symbol, args.start_date, args.end_date)