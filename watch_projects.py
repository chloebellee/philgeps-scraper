# watch_projects.py
#
# Watches PhilGEPS for specific projects, independent of the business-line
# keyword filter in philgeps_scrape.py. The site's own keyword search returns
# nothing (even for "software"), so this pages through every result in a set
# of categories where such a project could be filed and matches on the title.
# First-seen hits are appended to watchlist_hits.csv and printed as "NEW HIT".
#
# Usage:
#   python watch_projects.py --headless

import argparse
import csv
import re
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

from philgeps_scrape import (
    OPPS_URL, MAX_PAGES_PER_KEYWORD, click_next_page, pick_best_node,
    wait_for_results, absolutize, extract_refid_from_string, text_clean,
)

OUT = Path(__file__).parent / "watchlist_hits.csv"
FIELDS = ["Watch", "Reference/Solicitation No.", "Title", "URL", "First Seen"]

# IT, integration, internet/telecom, consulting, general services, training
WATCH_CATEGORIES = [
    ("10", "Information Technology"), ("108", "Systems Integration"),
    ("86", "Internet Services"), ("167", "IT Broadcasting and Telecommunications"),
    ("43", "Consulting Services"), ("23", "Services"),
    ("178", "Education and Training Services"),
]

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

    new_rows, failed = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context()
        for cat_id, cat_name in WATCH_CATEGORIES:
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

    if new_rows:
        write_header = not OUT.exists()
        with open(OUT, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
    if failed:
        print(f"WARNING: could not scan: {', '.join(failed)}")
    print(f"Done. {len(new_rows)} new hit(s) total.")


if __name__ == "__main__":
    main()
