# watch_projects.py
#
# Watches PhilGEPS for specific projects, independent of the business-line
# keyword filter in philgeps_scrape.py. The site's own keyword search returns
# nothing (even for "software"), so this walks every PhilGEPS category (the
# dropdown is read live, so new categories are picked up) and matches on the
# title. It also scans the PS-DBM portal (ps-philgeps.gov.ph), where big
# ERP/IT projects run by PS-DBM are posted instead of on the main site.
# First-seen hits are appended to watchlist_hits.csv and printed as "NEW HIT".
#
# Usage:
#   python watch_projects.py --headless

import argparse
import csv
import re
from datetime import date
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from philgeps_scrape import (
    OPPS_URL, MAX_PAGES_PER_KEYWORD, click_next_page, pick_best_node,
    wait_for_results, absolutize, extract_refid_from_string, text_clean,
    make_detail_urls, scrape_detail_info, extract_abc_from_info, info_pick,
    parse_abc_numeric, match_business_line,
)

OUT = Path(__file__).parent / "watchlist_hits.csv"
FIELDS = ["Watch", "Reference/Solicitation No.", "Title", "URL", "First Seen"]

# Categories that always get scanned first (most likely homes for an ERP notice);
# every other category in the dropdown is scanned after these.
PRIORITY_CATEGORIES = ["10", "108", "86", "167", "11", "43", "23", "178"]

# name -> regex the result title must match
WATCHES = {
    # Bare "DHSUD" also appears in unrelated LGU notices ("DHSUD Standard"), so
    # require it alongside ERP wording.
    "DHSUD ERP": re.compile(
        r"enterprise resource planning"
        r"|(dhsud|human settlements).*\berp\b|\berp\b.*(dhsud|human settlements)",
        re.I,
    ),
}


def list_categories(ctx):
    """Return [(id, name)] for every category in the Detailed Search dropdown."""
    page = ctx.new_page()
    try:
        page.goto(OPPS_URL, timeout=60000, wait_until="domcontentloaded",
                  referer="https://notices.philgeps.gov.ph/")
        page.wait_for_timeout(2000)
        page.evaluate("__doPostBack('lbtnDetailed','')")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)
        opts = page.eval_on_selector_all(
            "select[name='lstCategory'] option",
            "els => els.map(e => [e.value, e.textContent.trim()])")
    finally:
        page.close()
    seen, cats = set(), []
    for cid, name in opts:
        if cid and cid not in seen:
            seen.add(cid)
            cats.append((cid, name))
    first = [c for c in cats if c[0] in PRIORITY_CATEGORIES]
    first.sort(key=lambda c: PRIORITY_CATEGORIES.index(c[0]))
    return first + [c for c in cats if c[0] not in PRIORITY_CATEGORIES]


def scan_ps_portal(seen, new_rows, main_rows):
    """Match watch patterns against the PS-DBM portal's Invitation to Bid list."""
    from ps_philgeps_scrape import list_items, BASE_URL
    print("[PS-DBM portal] scanning...")
    items = list_items()
    for title, url, item_id in items:
        for name, pattern in WATCHES.items():
            ref = f"PSDBM-{item_id}"
            if not pattern.search(title) or (name, ref) in seen:
                continue
            seen.add((name, ref))
            new_rows.append({
                "Watch": name, "Reference/Solicitation No.": ref,
                "Title": title, "URL": url, "First Seen": date.today().isoformat(),
            })
            print(f"NEW HIT [{name}] {ref}: {title}\n  {url}")
            main_rows.append({
                "Project/Title": title, "Procuring Entity": "", "Classification": "",
                "Category": "", "Procurement Mode": "", "ABC": "", "ABC_Numeric": None,
                "Area of Delivery": "", "Posting Date": "", "Closing/Deadline": "",
                "Reference/Solicitation No.": ref,
                "Business Line": match_business_line(title) or "software_it",
                "URL": url, "Detail URL Used": "",
            })
    print(f"  scanned {len(items)} PS-DBM notices.")


def build_main_row(ctx, refid, title, url):
    """Full main-data row for a watch hit (same columns as philgeps_scrape), skipping its filters."""
    detail_used, info = "", {}
    for u in make_detail_urls(url, refid):
        info = scrape_detail_info(ctx, u)
        if info:
            detail_used = u
            break
    project = info.get("procurement project", "") or info.get("title", "") or title
    category = info.get("category", "")
    abc = extract_abc_from_info(info)
    return {
        "Project/Title": text_clean(project),
        "Procuring Entity": text_clean(info.get("procuring entity", "")),
        "Classification": text_clean(info.get("classification", "")),
        "Category": text_clean(category),
        "Procurement Mode": text_clean(info.get("procurement mode", "") or info.get("mode of procurement", "")),
        "ABC": text_clean(abc),
        "ABC_Numeric": parse_abc_numeric(abc),
        "Area of Delivery": text_clean(info.get("area of delivery", "")),
        "Posting Date": text_clean(info_pick(info, "posting date", "date published", "date issued", "date posted")),
        "Closing/Deadline": text_clean(info_pick(
            info, "closing date", "closing date / time", "closing date/time",
            "closing date & time", "deadline of submission", "closing date and time")),
        "Reference/Solicitation No.": text_clean(
            info.get("reference number", "") or info.get("solicitation number", "") or refid),
        "Business Line": match_business_line(project, category) or "software_it",
        "URL": url,
        "Detail URL Used": detail_used,
    }


def add_to_main_data(rows):
    """Merge watch hits into the main CSV and append them to today's date tab in the Sheet.

    Appends (never clears) the tab: the main scraper has already written today's rows there.
    """
    if not rows:
        return
    from ps_philgeps_scrape import save_csv_only
    from philgeps_scrape import CREDENTIALS_FILE, SHEETS_ID
    save_csv_only(rows)
    if not CREDENTIALS_FILE.exists():
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"])
        sheet = gspread.authorize(creds).open_by_key(SHEETS_ID)
        tab = date.today().strftime("%B %-d")
        cols = list(rows[0].keys())
        try:
            ws = sheet.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet(title=tab, rows=500, cols=20)
            sheet.reorder_worksheets([ws] + [w for w in sheet.worksheets() if w.title != tab])
            ws.append_row(cols)
        ws.append_rows([["" if r[c] is None else r[c] for c in cols] for r in rows],
                       value_input_option="RAW")
        print(f"Google Sheets -> tab '{tab}': {len(rows)} watch hit(s) appended.")
    except Exception as e:
        print(f"  Daily-tab upload failed: {e}")


def upload_hits(rows):
    """Append new hits to a 'Watchlist' tab in the Google Sheet (skipped without credentials)."""
    from philgeps_scrape import CREDENTIALS_FILE, SHEETS_ID
    if not rows or not CREDENTIALS_FILE.exists():
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"])
        sheet = gspread.authorize(creds).open_by_key(SHEETS_ID)
        try:
            ws = sheet.worksheet("Watchlist")
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet(title="Watchlist", rows=200, cols=len(FIELDS))
            ws.append_row(FIELDS)
        ws.append_rows([[r[f] for f in FIELDS] for r in rows], value_input_option="RAW")
        print(f"Google Sheets -> 'Watchlist' tab: {len(rows)} hit(s) added.")
    except Exception as e:
        print(f"  Watchlist upload failed: {e}")


def search(ctx, cat_id):
    page = ctx.new_page()
    page.goto(OPPS_URL, timeout=60000, wait_until="domcontentloaded",
              referer="https://notices.philgeps.gov.ph/")
    page.wait_for_timeout(2000)
    page.evaluate("__doPostBack('lbtnDetailed','')")
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.wait_for_timeout(2000)
    page.select_option("select[name='lstCategory']", value=cat_id)
    page.evaluate("document.getElementById('btnSearch').click()")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=45000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    return page


def collect(page):
    """Yield (refid, title, url) for every result link across all pages."""
    for _ in range(MAX_PAGES_PER_KEYWORD):
        node = pick_best_node(page)
        if not wait_for_results(node, timeout_ms=8000):
            break
        for a in node.query_selector_all("a[href*='refID'], a[href*='refid']"):
            href = absolutize(node.url, a.get_attribute("href") or "")
            refid = extract_refid_from_string(href)
            title = text_clean(a.inner_text())
            if refid and title:
                yield refid, title, href
        if not click_next_page(page):
            break
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(900)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    seen = set()
    if OUT.exists():
        with open(OUT, newline="", encoding="utf-8") as f:
            seen = {(r["Watch"], r["Reference/Solicitation No."]) for r in csv.DictReader(f)}

    new_rows, main_rows, failed = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context()
        try:
            categories = list_categories(ctx)
        except Exception as e:
            print(f"  could not read category list ({type(e).__name__}); using priority list")
            categories = [(c, c) for c in PRIORITY_CATEGORIES]
        print(f"Scanning {len(categories)} categories.")
        for cat_id, cat_name in categories:
            print(f"[{cat_name}] scanning...")
            total = 0
            for attempt in range(1, 4):  # the site times out often
                page = None
                try:
                    page = search(ctx, cat_id)
                    for refid, title, url in collect(page):
                        total += 1
                        for name, pattern in WATCHES.items():
                            if not pattern.search(title) or (name, refid) in seen:
                                continue
                            seen.add((name, refid))
                            new_rows.append({
                                "Watch": name, "Reference/Solicitation No.": refid,
                                "Title": title, "URL": url, "First Seen": date.today().isoformat(),
                            })
                            print(f"NEW HIT [{name}] {refid}: {title}\n  {url}")
                            try:
                                main_rows.append(build_main_row(ctx, refid, title, url))
                            except Exception as e:
                                print(f"  could not fetch details ({type(e).__name__}); using title only")
                                main_rows.append({
                                    "Project/Title": title, "Procuring Entity": "",
                                    "Classification": "", "Category": "", "Procurement Mode": "",
                                    "ABC": "", "ABC_Numeric": None, "Area of Delivery": "",
                                    "Posting Date": "", "Closing/Deadline": "",
                                    "Reference/Solicitation No.": refid,
                                    "Business Line": match_business_line(title) or "software_it",
                                    "URL": url, "Detail URL Used": "",
                                })
                    print(f"  scanned {total} notices.")
                    break
                except Exception as e:
                    print(f"  attempt {attempt} failed: {type(e).__name__}")
                finally:
                    if page:
                        page.close()
            else:
                failed.append(cat_name)
        browser.close()

    try:
        scan_ps_portal(seen, new_rows, main_rows)
    except Exception as e:
        print(f"  PS-DBM portal scan failed: {type(e).__name__}: {e}")
        failed.append("PS-DBM portal")

    if new_rows:
        write_header = not OUT.exists()
        with open(OUT, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
        upload_hits(new_rows)
        add_to_main_data(main_rows)
    if failed:
        print(f"WARNING: could not scan: {', '.join(failed)}")
    print(f"Done. {len(new_rows)} new hit(s) total.")


if __name__ == "__main__":
    main()
