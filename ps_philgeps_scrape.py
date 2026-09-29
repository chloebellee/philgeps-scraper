# ps_philgeps_scrape.py
#
# Second source scraper: PS-DBM's own procurement portal (ps-philgeps.gov.ph).
#
# This is a *different* site from notices.philgeps.gov.ph (the one
# philgeps_scrape.py covers). The Procurement Service of the DBM (PS-DBM)
# runs its own common-use-supplies store AND acts as the Bids and Awards
# Committee / facilitator for big-ticket IT/ERP/systems projects that other
# agencies (e.g. BSP, BIR) commission it to run. Those never show up on the
# main PhilGEPS "Open Opportunities" search, so philgeps_scrape.py can never
# find them no matter how the category/keyword filters are tuned.
#
# Unlike the main site (ASP.NET, requires Playwright), this one is a plain
# server-rendered Joomla site, so a simple requests+BeautifulSoup crawl is
# enough — no browser automation needed.
#
# Usage:
#   python ps_philgeps_scrape.py
#
# Notices here are posted as embedded PDFs (scanned/signed documents), so
# ABC, procuring entity, and closing date generally aren't extractable from
# the HTML — only the title and a link to the source PDF. Rows are still
# appended to the same philgeps_results_public.csv so they show up
# alongside the main-portal results; the Classification column is tagged
# "PS-DBM Portal" so you can tell them apart.

import re
import time
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from philgeps_scrape import OUT_CSV, text_clean, match_business_line, ALLOWED_LOBS

BASE_URL = "https://ps-philgeps.gov.ph"
SECTION_PATH = "/home/index.php/bid-opportunities/invitation-to-bid"
SECTION_NAME = "PS-DBM Portal"

MAX_PAGES = 10          # 20 items/page -> up to 200 most-recent items
PAGE_SIZE = 20
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_REQUESTS = 0.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

REF_RX = re.compile(
    r"\b(?:PB\s*No\.?\s*\d{2,4}-\d{2,4}(?:-\d+)?"
    r"|SVP-\d{4}-\d+"
    r"|APC-\d+-\d{2}"
    r"|PB-\d{4}-\d+)\b",
    re.IGNORECASE,
)

ITEM_LINK_RX = re.compile(r"/bid-opportunities/invitation-to-bid/(\d+)-")


def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def list_items(max_pages: int = MAX_PAGES) -> list:
    """Walk the paginated Invitation to Bid listing, return [(title, detail_url, item_id)]."""
    items, seen_ids = [], set()
    for page_num in range(max_pages):
        start = page_num * PAGE_SIZE
        url = f"{BASE_URL}{SECTION_PATH}" + (f"?start={start}" if start else "")
        try:
            html = fetch(url)
        except requests.RequestException as e:
            print(f"  [ps-philgeps] page {page_num}: request failed ({e})")
            break

        soup = BeautifulSoup(html, "lxml")
        page_items = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = ITEM_LINK_RX.search(href)
            if not m:
                continue
            item_id = m.group(1)
            title = text_clean(a.get_text(" "))
            if not title or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            page_items.append((title, urljoin(BASE_URL, href), item_id))

        print(f"  [ps-philgeps] page {page_num} (start={start}): {len(page_items)} item(s).")
        if not page_items:
            break
        items.extend(page_items)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return items


def extract_pdf_url(detail_url: str) -> str:
    try:
        html = fetch(detail_url)
    except requests.RequestException:
        return ""
    soup = BeautifulSoup(html, "lxml")
    iframe = soup.find("iframe", src=True)
    if iframe:
        return urljoin(BASE_URL, iframe["src"])
    link = soup.find("a", href=re.compile(r"\.pdf$", re.IGNORECASE))
    return urljoin(BASE_URL, link["href"]) if link else ""


def guess_procuring_entity(title: str) -> str:
    if re.search(r"for the procurement service\b", title, re.IGNORECASE):
        return "Procurement Service - DBM (PS-DBM)"
    return ""


def extract_reference(title: str, item_id: str) -> str:
    m = REF_RX.search(title)
    if m:
        return text_clean(m.group(0))
    return f"PSDBM-{item_id}"


def save_csv_only(rows: list) -> None:
    """
    Merge new rows into OUT_CSV directly (CSV only — no Google Sheets upload).

    philgeps_scrape.py's save_results() also uploads to a date-named Sheets
    tab; calling it a second time in the same run would clear and overwrite
    that tab with only this source's rows, wiping out whatever the main
    scraper just uploaded. Keeping this merge local avoids that collision.
    """
    COLS = [
        "Project/Title", "Procuring Entity", "Classification", "Category",
        "Procurement Mode", "ABC", "ABC_Numeric", "Area of Delivery", "Posting Date",
        "Closing/Deadline", "Reference/Solicitation No.", "Business Line",
        "URL", "Detail URL Used",
    ]
    new_df = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)

    if Path(OUT_CSV).exists():
        existing_df = pd.read_csv(OUT_CSV)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined.drop_duplicates(subset=["Reference/Solicitation No."], keep="first", inplace=True)
        combined.to_csv(OUT_CSV, index=False)
        print(f"CSV updated: {len(combined)} total rows in {OUT_CSV} ({len(new_df)} new).")
    else:
        new_df.to_csv(OUT_CSV, index=False)
        print(f"Saved {len(new_df)} rows to {OUT_CSV}")


def load_existing_refs() -> set:
    if not Path(OUT_CSV).exists():
        return set()
    try:
        existing_df = pd.read_csv(OUT_CSV)
        return set(existing_df["Reference/Solicitation No."].dropna().astype(str))
    except Exception:
        return set()


def run() -> list:
    print("\n=== PS-DBM Portal Scraper (ps-philgeps.gov.ph) ===")
    existing_refs = load_existing_refs()
    print(f"Loaded {len(existing_refs)} existing reference numbers from {OUT_CSV}")

    raw_items = list_items()
    print(f"Total items found: {len(raw_items)}")

    rows = []
    skipped_lob, skipped_dup = 0, 0
    for title, detail_url, item_id in raw_items:
        lob = match_business_line(title)
        if lob not in ALLOWED_LOBS:
            skipped_lob += 1
            continue

        ref = extract_reference(title, item_id)
        if ref in existing_refs:
            skipped_dup += 1
            continue

        pdf_url = extract_pdf_url(detail_url)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        rows.append({
            "Project/Title": title,
            "Procuring Entity": guess_procuring_entity(title),
            "Classification": SECTION_NAME,
            "Category": "",
            "Procurement Mode": "",
            "ABC": "",
            "ABC_Numeric": None,
            "Area of Delivery": "",
            "Posting Date": "",
            "Closing/Deadline": "",
            "Reference/Solicitation No.": ref,
            "Business Line": lob,
            "URL": detail_url,
            "Detail URL Used": pdf_url,
        })

    print(
        f"Kept {len(rows)} new item(s) "
        f"(skipped {skipped_lob} off-topic, {skipped_dup} already known)."
    )
    return rows


if __name__ == "__main__":
    new_rows = run()
    save_csv_only(new_rows)
