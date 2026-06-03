# philgeps_scrape.py
#
# PhilGEPS public scraper (no login) — portal-tab aware + auto search + auto-pagination.
#
# Usage (manual, original behaviour):
#   python philgeps_scrape.py
#
# Usage (fully automatic — searches Marketing, IT, Video/Photo, paginates itself):
#   python philgeps_scrape.py --auto
#   python philgeps_scrape.py --auto --headless   ← for scheduled/background runs
#
# Scheduled daily runs (Mon–Fri 9 AM):
#   python scheduler.py

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import pandas as pd
import re, argparse
from urllib.parse import urlparse, parse_qs, urljoin
from pathlib import Path

# ------------------------------ CONFIG ------------------------------

HOME_URL    = "https://www.philgeps.gov.ph/"
PORTAL_ROOT = "https://notices.philgeps.gov.ph/"
OUT_CSV     = "philgeps_results_public.csv"

DEBUG     = True
HEADLESS  = False   # overridden by --headless flag
AUTO_MODE = False   # overridden by --auto flag

MAX_PAGES_PER_KEYWORD = 15  # safety cap on auto-pagination per search term

# One search pass per entry — each is sent to the PhilGEPS keyword field.
# The existing KEYWORDS/ALLOWED_LOBS filter removes anything unrelated.
SEARCH_PASSES = [
    "marketing",
    "information technology",
    "software",
    "video production",
    "photography",
    "videography",
    "digital marketing",
    "advertising",
]

# PhilGEPS search/opportunities URLs to try (in order)
PORTAL_SEARCH_URLS = [
    "https://notices.philgeps.gov.ph/GEPSNONPILOT/Tender/SplashOpenOpportunitiesUI.aspx",
    "https://notices.philgeps.gov.ph/GEPSNONPILOT/Tender/OpenDetailedSearchUI.aspx",
]

# Keep ONLY these business lines
ALLOWED_LOBS = {"software_it", "marketing", "events_photo"}

# Tight keywords (avoid generic "system")
KEYWORDS = {
    "software_it": [
        "software", "web", "website", "mobile", "app", "application",
        "developer", "programmer",
        "database", "mis", "cms", "lms", "erp",
        "it support", "information system", "it system",
        "cybersecurity", "cloud", "api", "integration"
    ],
    "marketing": [
        "marketing", "branding", "campaign", "social media", "digital marketing",
        "content", "media placement", "ads", "advertising", "collateral",
        "public relations", "pr"
    ],
    "events_photo": [
        "event coverage", "photo coverage", "photography", "videography",
        "documentation", "livestream", "avp", "video production",
        "conference", "expo", "event management"
    ],
}

# Hard exclusions
EXCLUDE_IF_CATEGORY = {
    "construction projects", "civil works",
    "hardware and construction supplies",
    "construction materials", "construction materials and supplies",
    "infrastructure projects", "roads and highways"
}
EXCLUDE_IF_TITLE = {
    "construction", "concreting", "rehabilitation", "reconstruction",
    "slope protection", "riprap", "drainage", "bridge", "road",
    "streetlight", "street light", "streetlights",
    "water system", "warehouse", "fencing", "hospital",
    "basketball", "school building", "building", "improvement"
}

REQUIRE_REGION_MATCH = False
REGION_TOKENS = {
    "`z`", "negros occidental", "capiz", "aklan", "antique", "guimaras",
    "western visayas", "region vi"
}

# ----------------------------- HELPERS -----------------------------

def text_clean(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def norm_key(k: str) -> str:
    k = text_clean(k).lower()
    k = re.sub(r"[：:]+$", "", k)
    return k

def parse_abc_numeric(abc_str: str):
    if not abc_str:
        return None
    s = abc_str.replace(",", "")
    s = s.replace("₱", "").replace("php", "").replace("Php", "").replace("PHP", "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None

def match_business_line(title: str, category_or_text: str = "") -> str:
    blob = f"{title or ''} | {category_or_text or ''}".lower()

    def has_kw(words):
        for w in words:
            if " " in w:
                if w in blob:
                    return True
            else:
                if re.search(rf"\b{re.escape(w)}\b", blob):
                    return True
        return False

    for tag, words in KEYWORDS.items():
        if has_kw(words):
            return tag
    return ""

def in_region_text(*chunks: str) -> bool:
    t = " | ".join(ch for ch in chunks if ch).lower()
    return any(tok in t for tok in REGION_TOKENS)

def extract_title(soup: BeautifulSoup) -> str:
    for sel in ["h1", "h2", "h3", "title"]:
        tag = soup.find(sel)
        if tag:
            t = text_clean(tag.get_text(" "))
            if t and "bid notice abstract" not in t.lower():
                return t
    return ""

def extract_kv_from_tables(soup: BeautifulSoup) -> dict:
    info = {}
    for tbl in soup.find_all("table"):
        for tr in tbl.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) == 2:
                k = norm_key(cells[0].get_text(" "))
                v = text_clean(cells[1].get_text(" "))
                if k and v:
                    info[k] = v
            elif len(cells) == 1:
                line = text_clean(cells[0].get_text(" "))
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = norm_key(k)
                    v = text_clean(v)
                    if k and v:
                        info[k] = v
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            k = norm_key(dt.get_text(" "))
            v = text_clean(dd.get_text(" "))
            if k and v:
                info[k] = v
    info["_fulltext"] = soup.get_text(" ", strip=True)
    t = extract_title(soup)
    if t and "title" not in info:
        info["title"] = t
    return info

def info_pick(info: dict, *aliases: str) -> str:
    if not info:
        return ""
    keys = list(info.keys())
    for name in aliases:
        n = norm_key(name)
        if n in info:
            return info[n]
        for k in keys:
            if n == norm_key(k):
                return info[k]
    for name in aliases:
        n = norm_key(name)
        for k in keys:
            if all(tok in norm_key(k) for tok in n.split() if tok):
                return info[k]
    return ""

def extract_abc_from_info(info: dict) -> str:
    if not info:
        return ""
    abc = info_pick(info, "approved budget for the contract", "abc", "budget (php)", "budget")
    if abc:
        return text_clean(abc)
    full = info.get("_fulltext", "") or ""
    m = re.search(r"Budget\s*\(PHP\)\s*[:\-]?\s*([₱A-Z\s]*[\d][\d,\.]*)", full, re.I)
    if m:
        return text_clean(m.group(1))
    m = re.search(r"Approved Budget for the Contract\s*[:\-]?\s*([₱A-Z\s]*[\d][\d,\.]*)", full, re.I)
    if m:
        return text_clean(m.group(1))
    m = re.search(r"\bABC\b\s*[:\-]?\s*([₱A-Z\s]*[\d][\d,\.]*)", full, re.I)
    if m:
        return text_clean(m.group(1))
    return ""

def absolutize(base_url: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(base_url.rstrip("/") + "/", href)

def pick_results_tab(ctx):
    pages = ctx.pages
    if not pages:
        return None
    candidates = []
    for p in pages:
        u = (p.url or "").lower()
        if any(k in u for k in ["notices.philgeps.gov.ph", "opportun", "abstract", "search", "gepsnonpilot", "tender"]):
            candidates.append(p)
    return candidates[-1] if candidates else pages[-1]

def pick_best_node(page):
    try:
        for fr in page.frames[::-1]:
            u = (fr.url or "").lower()
            if any(k in u for k in ["opportun", "searchui", "notices", "gepsnonpilot", "tender"]):
                return fr
        return page
    except Exception:
        return page

def ensure_portal_tab(ctx, page):
    try:
        anchors = page.query_selector_all("a")
        candidate = None
        for a in anchors:
            t = (a.inner_text() or "").strip().lower()
            if "click" in t and "here" in t:
                candidate = a
                break
        if candidate:
            try:
                with page.expect_popup() as pinfo:
                    candidate.click()
                portal = pinfo.value
                portal.wait_for_load_state("domcontentloaded", timeout=15000)
                return portal
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
                return page
        else:
            portal = ctx.new_page()
            portal.goto(PORTAL_ROOT, timeout=20000)
            return portal
    except Exception:
        portal = ctx.new_page()
        portal.goto(PORTAL_ROOT, timeout=20000)
        return portal

def try_enter_detailed_search(ctx, page):
    targets_text = ["Detailed Search", "Advanced Search", "Detailed", "Advanced", "Search"]
    targets_href = ["DetailedSearch", "OpenDetailedSearch", "SearchUI", "Opportunit", "OpenOpp", "OpenOpportun"]

    def click_candidates(node):
        for txt in targets_text:
            try:
                loc = node.locator(f"a:has-text('{txt}')")
                if loc.count() > 0:
                    if DEBUG:
                        print(f"  - Clicking link with text: {txt}")
                    try:
                        loc.first.click(timeout=3000)
                        return True
                    except Exception:
                        pass
                locb = node.locator(f"button:has-text('{txt}')")
                if locb.count() > 0:
                    if DEBUG:
                        print(f"  - Clicking button with text: {txt}")
                    try:
                        locb.first.click(timeout=3000)
                        return True
                    except Exception:
                        pass
            except Exception:
                pass
        for a in node.query_selector_all("a"):
            href = a.get_attribute("href") or ""
            if any(key.lower() in href.lower() for key in targets_href):
                if DEBUG:
                    print(f"  - Clicking link by href match: {href[:80]}...")
                try:
                    a.click(timeout=3000)
                    return True
                except Exception:
                    pass
        return False

    try:
        for fr in page.frames[::-1]:
            if click_candidates(fr):
                fr.wait_for_timeout(800)
                return True
    except Exception:
        pass
    try:
        if click_candidates(page):
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass
    try:
        if DEBUG:
            print("  - Opening neutral portal root as fallback.")
        newp = ctx.new_page()
        newp.goto(PORTAL_ROOT, timeout=10000)
        newp.wait_for_load_state("domcontentloaded", timeout=8000)
        return True
    except Exception:
        return False

# ------------------ PUBLIC SCRAPE (NO LOGIN) ------------------

REFID_RX = re.compile(r"refid=(\d+)", re.IGNORECASE)

def extract_refid_from_string(s: str) -> str:
    if not s:
        return ""
    m = REFID_RX.search(s)
    return m.group(1) if m else ""

def make_detail_urls(base_like: str, refid: str):
    if not refid:
        return []
    parsed = urlparse(base_like or PORTAL_ROOT)
    host = f"{parsed.scheme}://{parsed.netloc}"
    return [
        f"{host}/GEPSNONPILOT/Tender/SplashBidNoticeAbstractUI.aspx?refID={refid}",
        f"{host}/GEPSNONPILOT/Tender/PrintableBidNoticeAbstractUI.aspx?refID={refid}",
    ]

def scrape_detail_info(ctx, url: str) -> dict:
    try:
        pg = ctx.new_page()
        pg.goto(url, timeout=30000, wait_until="domcontentloaded")
        pg.wait_for_timeout(700)
        html = pg.content()
        pg.close()
        soup = BeautifulSoup(html, "lxml")
        info = extract_kv_from_tables(soup)
        valid_keys = {"approved budget for the contract", "abc", "procuring entity", "area of delivery"}
        return info if any(k in info for k in valid_keys) or info.get("_fulltext") else {}
    except Exception:
        return {}

def wait_for_results(node, timeout_ms=6000):
    try:
        node.wait_for_selector("a[href*='refID'], a[href*='refid'], a[href*='BidNoticeAbstract']", timeout=timeout_ms)
        return True
    except Exception:
        try:
            node.wait_for_selector("tr, div[role='row'], div[aria-rowindex]", timeout=2000)
            return True
        except Exception:
            return False

def find_refid_result_links(node) -> list:
    links, seen = [], set()
    selectors = [
        "a[href*='refID']", "a[href*='refid']",
        "a[href*='SplashBidNoticeAbstractUI']",
        "a[href*='BidNoticeAbstract']",
    ]
    for sel in selectors:
        for a in node.query_selector_all(sel):
            href = a.get_attribute("href") or ""
            if not href:
                continue
            href_full = absolutize(node.url, href)
            if href_full in seen:
                continue
            seen.add(href_full)
            links.append(href_full)
    return links

def gather_from_results_public(ctx, page) -> list:
    node = pick_best_node(page)
    if not wait_for_results(node, timeout_ms=3000):
        if DEBUG:
            print("  (Results not detected — attempting to open Detailed/Advanced Search...)")
        try_enter_detailed_search(ctx, page)
        node = pick_best_node(pick_results_tab(ctx) or page)
        if not wait_for_results(node, timeout_ms=5000):
            if DEBUG:
                print("  (Still no results detected on this tab/frame.)")
            return []

    candidates = find_refid_result_links(node)
    if DEBUG:
        print(f"  Found {len(candidates)} refID links on this page.")
        for u in candidates[:5]:
            print("   •", u)

    if not candidates:
        return []

    items = []
    for href_full in candidates:
        refid = extract_refid_from_string(href_full)
        if not refid:
            try:
                parsed = urlparse(href_full)
                qs = parse_qs(parsed.query)
                df = qs.get("DirectFrom") or qs.get("directfrom")
                if df:
                    refid = extract_refid_from_string(df[0])
            except Exception:
                pass

        detail_used, info = "", {}
        for u in make_detail_urls(href_full, refid):
            info = scrape_detail_info(ctx, u)
            if info:
                detail_used = u
                break

        project = info.get("procurement project", "") or info.get("title", "")
        entity = info.get("procuring entity", "")
        classification = info.get("classification", "")
        category = info.get("category", "")
        mode = info.get("procurement mode", "") or info.get("mode of procurement", "")
        abc = extract_abc_from_info(info)
        area = info.get("area of delivery", "")

        posting = info_pick(info, "posting date", "date published", "date issued", "date posted")
        closing = info_pick(
            info,
            "closing date", "closing date / time", "closing date/time",
            "closing date & time", "deadline of submission", "closing date and time"
        )
        refno = info.get("reference number", "") or info.get("solicitation number", "") or (refid or "")

        lob = match_business_line(project, category or "")
        if lob not in ALLOWED_LOBS:
            continue

        cat_norm = (category or "").strip().lower()
        if cat_norm in EXCLUDE_IF_CATEGORY or any(ex in cat_norm for ex in EXCLUDE_IF_CATEGORY):
            continue

        title_norm = (project or "").strip().lower()
        if any(term in title_norm for term in EXCLUDE_IF_TITLE):
            continue

        if REQUIRE_REGION_MATCH and not in_region_text(area, entity, project):
            continue

        items.append({
            "Project/Title": text_clean(project),
            "Procuring Entity": text_clean(entity),
            "Classification": text_clean(classification),
            "Category": text_clean(category),
            "Procurement Mode": text_clean(mode),
            "ABC": text_clean(abc),
            "ABC_Numeric": parse_abc_numeric(abc),
            "Area of Delivery": text_clean(area),
            "Posting Date": text_clean(posting),
            "Closing/Deadline": text_clean(closing),
            "Reference/Solicitation No.": text_clean(refno),
            "Business Line": lob,
            "URL": href_full,
            "Detail URL Used": detail_used,
        })

    return items

# ----------------------- AUTO MODE HELPERS -----------------------

def navigate_and_search(ctx, keyword: str):
    """
    Open a fresh page, navigate to PhilGEPS Open Opportunities, fill the
    keyword/title field with `keyword`, and submit. Returns the results page
    or None on failure.
    """
    page = ctx.new_page()
    try:
        # Try each search URL until one loads
        loaded = False
        for url in PORTAL_SEARCH_URLS:
            try:
                page.goto(url, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                loaded = True
                break
            except Exception:
                continue

        if not loaded:
            page.goto(PORTAL_ROOT, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

        # If we're on a splash/landing, click into Detailed/Advanced Search
        if not wait_for_results(pick_best_node(page), timeout_ms=1500):
            try_enter_detailed_search(ctx, page)
            page.wait_for_timeout(1000)

        # Try to fill the title/keyword search field
        field_selectors = [
            "input[name*='Title']",
            "input[name*='title']",
            "input[name*='Keyword']",
            "input[name*='keyword']",
            "input[id*='Title']",
            "input[id*='title']",
            "input[id*='Keyword']",
            "input[placeholder*='title']",
            "input[placeholder*='keyword']",
            "input[placeholder*='Title']",
        ]

        filled = False
        # Try on the page and its frames
        nodes = [page] + list(reversed(page.frames))
        for node in nodes:
            if filled:
                break
            for sel in field_selectors:
                try:
                    loc = node.locator(sel)
                    if loc.count() > 0:
                        loc.first.clear()
                        loc.first.fill(keyword)
                        filled = True
                        if DEBUG:
                            print(f"  Filled '{keyword}' → {sel}")
                        break
                except Exception:
                    pass

        if not filled:
            # Fallback: first visible text input
            for node in nodes:
                try:
                    inputs = node.locator("input[type='text']:visible")
                    if inputs.count() > 0:
                        inputs.first.clear()
                        inputs.first.fill(keyword)
                        filled = True
                        if DEBUG:
                            print(f"  Filled '{keyword}' → first visible text input")
                        break
                except Exception:
                    pass

        if not filled:
            if DEBUG:
                print(f"  WARNING: could not find search field for '{keyword}'. Trying to scrape current page anyway.")

        # Click Search / Submit button
        btn_selectors = [
            "input[value='Search']",
            "input[value='search']",
            "input[value='SEARCH']",
            "button:has-text('Search')",
            "a:has-text('Search')",
            "input[type='submit']",
            "button[type='submit']",
        ]
        for node in nodes:
            clicked = False
            for sel in btn_selectors:
                try:
                    btn = node.locator(sel)
                    if btn.count() > 0:
                        btn.first.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    pass
            if clicked:
                break

        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(1200)
        return page

    except Exception as e:
        if DEBUG:
            print(f"  Error in navigate_and_search('{keyword}'): {e}")
        try:
            page.close()
        except Exception:
            pass
        return None


def click_next_page(page) -> bool:
    """
    Click the Next page button/link in the results grid.
    Handles ASP.NET GridView __doPostBack pagination and plain "Next"/">" links.
    Returns True if a Next link was found and clicked.
    """
    next_selectors = [
        "a:has-text('Next')",
        "a:has-text('>')",
        "a:has-text('»')",
        "input[value='Next']",
        "input[value='>']",
        "a[href*='Page$Next']",
        "a[href*='Page%24Next']",
    ]

    nodes = [page] + list(reversed(page.frames))
    for node in nodes:
        for sel in next_selectors:
            try:
                loc = node.locator(sel)
                if loc.count() == 0:
                    continue
                # Skip if the element is inside a <span> (disabled current-page indicator)
                tag = loc.first.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    loc.first.click(timeout=5000)
                    return True
                elif tag == "input":
                    loc.first.click(timeout=5000)
                    return True
            except Exception:
                pass
    return False


def scrape_all_pages(ctx, page, keyword: str) -> list:
    """Scrape all result pages for a single keyword search, auto-paginating."""
    all_items = []
    for page_num in range(1, MAX_PAGES_PER_KEYWORD + 1):
        if DEBUG:
            print(f"  ['{keyword}'] page {page_num}...")

        node = pick_best_node(page)
        if not wait_for_results(node, timeout_ms=8000):
            if DEBUG:
                print(f"  No results detected on page {page_num}.")
            break

        items = gather_from_results_public(ctx, page)
        all_items.extend(items)
        if DEBUG:
            print(f"  Page {page_num}: {len(items)} matching item(s) kept.")

        if not click_next_page(page):
            if DEBUG:
                print(f"  No Next page — done with '{keyword}'.")
            break

        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(900)

    return all_items


def save_results(rows: list, append: bool = False):
    """Save rows to CSV. If append=True, merges with existing file and deduplicates."""
    COLS = [
        "Project/Title", "Procuring Entity", "Classification", "Category",
        "Procurement Mode", "ABC", "ABC_Numeric", "Area of Delivery", "Posting Date",
        "Closing/Deadline", "Reference/Solicitation No.", "Business Line",
        "URL", "Detail URL Used",
    ]

    new_df = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)

    if append and Path(OUT_CSV).exists():
        try:
            existing_df = pd.read_csv(OUT_CSV)
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            combined.drop_duplicates(subset=["Reference/Solicitation No."], keep="first", inplace=True)
            combined.to_csv(OUT_CSV, index=False)
            print(f"CSV updated: {len(combined)} total rows in {OUT_CSV} ({len(new_df)} new).")
            return
        except Exception as e:
            if DEBUG:
                print(f"  Could not merge with existing CSV ({e}). Overwriting.")

    if not new_df.empty:
        new_df.drop_duplicates(inplace=True)
    new_df.to_csv(OUT_CSV, index=False)
    print(f"Saved {len(new_df)} rows to {OUT_CSV}")


# ----------------------------- MODES -----------------------------

def auto_run():
    """
    Fully automated mode: for each keyword in SEARCH_PASSES, navigate to PhilGEPS,
    fill the search form, scrape all result pages, then save/append to CSV.
    """
    print("\n=== PhilGEPS Auto Scraper ===")
    print(f"Search passes : {SEARCH_PASSES}")
    print(f"Headless      : {HEADLESS}")
    print(f"Max pages/kw  : {MAX_PAGES_PER_KEYWORD}\n")

    # Pre-load existing reference numbers to skip duplicates
    existing_refs: set = set()
    if Path(OUT_CSV).exists():
        try:
            existing_df = pd.read_csv(OUT_CSV)
            existing_refs = set(existing_df["Reference/Solicitation No."].dropna().astype(str))
            print(f"Loaded {len(existing_refs)} existing reference numbers from {OUT_CSV}\n")
        except Exception:
            pass

    all_new_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context()

        for keyword in SEARCH_PASSES:
            print(f"\n--- Keyword: '{keyword}' ---")
            search_page = navigate_and_search(ctx, keyword)
            if not search_page:
                print(f"  Skipping '{keyword}' (navigation failed).")
                continue

            items = scrape_all_pages(ctx, search_page, keyword)

            try:
                search_page.close()
            except Exception:
                pass

            # Drop items already in the CSV
            new_items = [
                item for item in items
                if str(item.get("Reference/Solicitation No.", "")) not in existing_refs
            ]
            for item in new_items:
                ref = str(item.get("Reference/Solicitation No.", ""))
                if ref:
                    existing_refs.add(ref)

            all_new_rows.extend(new_items)
            print(f"  Kept {len(new_items)} new item(s) "
                  f"(skipped {len(items) - len(new_items)} duplicate(s)).")

        browser.close()

    print(f"\nTotal new rows this run: {len(all_new_rows)}")
    save_results(all_new_rows, append=True)
    return all_new_rows


def manual_run():
    """Original manual mode — user sets up filters in the browser, presses ENTER to scrape."""
    print("\n=== PhilGEPS Public Scraper (manual mode) ===")
    print("In the portal tab, open Open Opportunities → Detailed/Advanced Search,")
    print("set your filters, then press ENTER here to scrape.")
    print("Click Next in the browser → ENTER again. Type 'q' to finish.\n")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        home = ctx.new_page()
        home.goto(HOME_URL, timeout=20000)

        portal = ensure_portal_tab(ctx, home)
        print(f"Portal tab: {portal.url}")

        while True:
            ans = input("➡️  When the RESULTS LIST is visible, press ENTER to scrape (or 'q' to finish): ").strip().lower()
            if ans == "q":
                break

            current_page = pick_results_tab(ctx)
            if not current_page:
                print("No tab found. Make sure the portal opened and is visible.")
                continue

            print(f"Using tab: {current_page.url}")
            collected = gather_from_results_public(ctx, current_page)

            if collected:
                rows.extend(collected)
                print(f"  Collected {len(collected)} items on this page (after filtering).")
            else:
                print("  No matching items on this page (after filtering).")

            print("\nIf more results exist, click NEXT in the browser, then press ENTER again.")
            print("Or type 'q' to finish.\n")

        browser.close()

    save_results(rows, append=False)

    if not rows:
        print("Note: If the CSV is empty, you were likely on a splash/landing view.")
        print("Enter Detailed/Advanced Search, run a query, ensure the list shows rows, then press ENTER.")


# ------------------------------ MAIN ------------------------------

def main():
    global AUTO_MODE, HEADLESS

    parser = argparse.ArgumentParser(
        description="PhilGEPS scraper — manual or fully automatic.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Automatically search for Marketing/IT/Video-Photo categories and paginate."
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run the browser in headless mode (required for scheduled/background runs)."
    )
    args = parser.parse_args()

    if args.auto:
        AUTO_MODE = True
    if args.headless:
        HEADLESS = True

    if AUTO_MODE:
        auto_run()
    else:
        manual_run()


if __name__ == "__main__":
    main()
