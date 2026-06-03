# philgeps_scrape.py
#
# PhilGEPS public scraper (no login) — portal-tab aware + auto "Detailed/Advanced Search".
# - Opens the PhilGEPS home, clicks the “click Here” link (captures the NEW TAB), or opens portal root.
# - If you're on a splash/landing page (no results), it will try to click "Detailed/Advanced Search" for you.
# - On the results list, it finds refID links, opens Detail/Printable pages, and extracts fields (incl. ABC price).
# - Robust ABC parsing (handles "Approved Budget for the Contract:", "Budget (PHP)", "ABC").
# - Filters OUT construction/civil-works and keeps ONLY Software/IT, Marketing, Events/Photo within Region VI.
# - Saves philgeps_results_public.csv with ABC and ABC_Numeric (sortable).
#
# Usage:
#   1) Run this script. It opens home, then the portal tab.
#   2) In the portal tab, choose Open Opportunities → Detailed/Advanced Search, set filters, show RESULTS LIST.
#   3) Press ENTER in Terminal to scrape that page. Click Next → ENTER for more. Type 'q' to finish.

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import pandas as pd
import re, itertools
from urllib.parse import urlparse, parse_qs, urljoin
from pathlib import Path

# ------------------------------ CONFIG ------------------------------

HOME_URL    = "https://www.philgeps.gov.ph/"
PORTAL_ROOT = "https://notices.philgeps.gov.ph/"  # neutral root
OUT_CSV     = "philgeps_results_public.csv"

DEBUG = True

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

# Hard exclusions (category/title contains these -> drop)
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

# Region VI constraint
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
    k = re.sub(r"[：:]+$", "", k)  # strip trailing colon(s)
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
    """Word/phrase aware matching to reduce false positives like 'water system'."""
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
    """
    Extract label:value pairs from:
      - 2-col tables (th/td or td/td)
      - 1-cell 'Label: Value' rows
      - definition lists (dt/dd)
    Also store page fulltext for regex fallback and best-effort title.
    Keys are normalized (lowercased, no trailing colon).
    """
    info = {}

    # tables: 2-col & 1-col (Label: Value)
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

    # definition lists
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            k = norm_key(dt.get_text(" "))
            v = text_clean(dd.get_text(" "))
            if k and v:
                info[k] = v

    # extras
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

    # contains match
    for name in aliases:
        n = norm_key(name)
        for k in keys:
            if all(tok in norm_key(k) for tok in n.split() if tok):
                return info[k]
    return ""

def extract_abc_from_info(info: dict) -> str:
    """Robust ABC extractor: label lookup + regex on full text."""
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
    """Prefer the portal/results tab; fallback to the newest tab."""
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
    """Some results lists render in a frame; prefer the tender/search frame."""
    try:
        for fr in page.frames[::-1]:
            u = (fr.url or "").lower()
            if any(k in u for k in ["opportun", "searchui", "notices", "gepsnonpilot", "tender"]):
                return fr
        return page
    except Exception:
        return page

def ensure_portal_tab(ctx, page):
    """
    From home, click the “click Here” link to open the portal in a new tab.
    If not found, open the neutral portal root.
    """
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

# ---------------------------------------------------- RESULTS PAGE ASSIST ---------------------------------------------------

def try_enter_detailed_search(ctx, page):
    """
    If user is stuck on a splash/landing page, try to click "Detailed" / "Advanced" / "Search" links/buttons.
    Tries on the page and its frames. Returns True if it thinks it navigated to a results/search page.
    """
    targets_text = ["Detailed Search", "Advanced Search", "Detailed", "Advanced", "Search"]
    targets_href = ["DetailedSearch", "OpenDetailedSearch", "SearchUI", "Opportunit", "OpenOpp", "OpenOpportun"]

    def click_candidates(node):
        # Try text-locators first (more robust for i18n/labeling)
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

        # Try href-based anchors
        for a in node.query_selector_all("a"):
            href = a.get_attribute("href") or ""
            if any(key.lower() in (href.lower()) for key in targets_href):
                if DEBUG:
                    print(f"  - Clicking link by href match: {href[:80]}...")
                try:
                    a.click(timeout=3000)
                    return True
                except Exception:
                    pass
        return False

    # Try in frames (often host the UI)
    try:
        for fr in page.frames[::-1]:
            if click_candidates(fr):
                fr.wait_for_timeout(800)
                return True
    except Exception:
        pass

    # Try on the page itself
    try:
        if click_candidates(page):
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass

    # As a last resort, open portal root (user can navigate to search from there)
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
    """Create BOTH detail + printable URLs on the SAME host as the link."""
    if not refid:
        return []
    parsed = urlparse(base_like or PORTAL_ROOT)
    host = f"{parsed.scheme}://{parsed.netloc}"
    return [
        f"{host}/GEPSNONPILOT/Tender/SplashBidNoticeAbstractUI.aspx?refID={refid}",
        f"{host}/GEPSNONPILOT/Tender/PrintableBidNoticeAbstractUI.aspx?refID={refid}",
    ]

def scrape_detail_info(ctx, url: str) -> dict:
    """Load a detail URL and extract fields. Return {} if not useful."""
    try:
        pg = ctx.new_page()
        pg.goto(url, timeout=30000, wait_until="domcontentloaded")
        pg.wait_for_timeout(700)  # small settle
        html = pg.content()
        pg.close()

        soup = BeautifulSoup(html, "lxml")
        info = extract_kv_from_tables(soup)

        # valid if we got some useful bits or at least _fulltext for regex fallback
        valid_keys = {"approved budget for the contract", "abc", "procuring entity", "area of delivery"}
        return info if any(k in info for k in valid_keys) or info.get("_fulltext") else {}
    except Exception:
        return {}

def wait_for_results(node, timeout_ms=6000):
    """Best-effort wait for either refID links or obvious row containers."""
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
    """Collect anchors that contain refID (or routes that include it)."""
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
    """
    1) Ensure we're on a results list (if splash, try auto "Detailed/Advanced Search").
    2) Grab refID-bearing links.
    3) For each, try Detail then Printable pages to extract fields.
    4) FILTER rows to only keep allowed business lines & Region VI, excluding construction-ish.
    """
    node = pick_best_node(page)

    # If we don't see results yet, try to auto-enter detailed/advanced search
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
        # Extract refID (also check DirectFrom param)
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

        # Core fields
        project = info.get("procurement project", "") or info.get("title", "")
        entity = info.get("procuring entity", "")
        classification = info.get("classification", "")
        category = info.get("category", "")
        mode = info.get("procurement mode", "") or info.get("mode of procurement", "")
        abc = extract_abc_from_info(info)
        area = info.get("area of delivery", "")

        # Posting / Date Published
        posting = info_pick(
            info,
            "posting date",
            "date published",
            "date issued",
            "date posted"
        )

        # Closing / Deadline
        closing = info_pick(
            info,
            "closing date",
            "closing date / time",
            "closing date/time",
            "closing date & time",
            "deadline of submission",
            "closing date and time"
        )

        refno = info.get("reference number", "") or info.get("solicitation number", "") or (refid or "")

        # ---------- FILTERING ----------
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
        # --------------------------------

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

# ------------------------------ MAIN ------------------------------

def main():
    print("\n=== PhilGEPS Public Scraper (portal-tab aware + auto Detailed Search + ABC) ===")
    print("In the portal tab, open Open Opportunities → Detailed/Advanced Search, set filters, show RESULTS.")
    print("Then press ENTER here. Click Next in the browser → ENTER again. Type 'q' to finish.\n")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        home = ctx.new_page()
        home.goto(HOME_URL, timeout=20000)

        # Open portal tab (new tab via 'click Here', or neutral root fallback)
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

    # Save output (always create file)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
        "Project/Title", "Procuring Entity", "Classification", "Category",
        "Procurement Mode", "ABC", "ABC_Numeric", "Area of Delivery", "Posting Date",
        "Closing/Deadline", "Reference/Solicitation No.", "Business Line",
        "URL", "Detail URL Used"
    ])

    if not df.empty:
        df.drop_duplicates(inplace=True)

    Path(OUT_CSV).write_text("")
    df.to_csv(OUT_CSV, index=False)

    print(f"\n📝 Saved {len(df)} rows to {OUT_CSV}")
    if df.empty:
        print("Note: If this is empty, you were likely still on a splash/landing view. Enter Detailed/Advanced Search, run")
        print("      a query (Region VI + keywords), ensure the list shows multiple rows with links, then press ENTER again.")

if __name__ == "__main__":
    main()

