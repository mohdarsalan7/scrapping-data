"""
Google Maps Scraper — Playwright (Zero API Cost)
=================================================
Scrapes Google Maps search results directly using a real browser.
No API key required. Completely free to run.

⚠️  Note: Scraping Google Maps may conflict with Google's Terms of Service.
    Use responsibly — add delays, don't hammer, don't resell raw data.
    For a fully compliant alternative, see osm_scraper.py

Install:
    pip install playwright gspread google-auth requests
    playwright install chromium

Run:
    python gmaps_playwright.py
"""

import os
import csv
import re
import time
import json
import logging
import asyncio
from datetime import datetime

import gspread
import requests
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ─────────────────────────────────────────────
# CONFIGURATION — edit before running
# ─────────────────────────────────────────────

SEARCH_QUERY        = "Garment Stores in New Delhi"  # Full search string
MAX_RESULTS         = 100           # How many listings to scrape (scroll until reached)
SCROLL_PAUSE_MS     = 1800         # Pause between scrolls (ms) — be polite
DETAIL_PAUSE_MS     = 1200         # Pause before reading each listing detail
HEADLESS            = True         # False = see the browser (useful for debugging)

SERVICE_ACCOUNT_FILE = "service_account.json"   # For writing to Google Sheets
SPREADSHEET_NAME     = "GMaps Leads (Free)"

# Email scraping
EMAIL_TIMEOUT_SEC    = 8
CONTACT_PATHS        = ["/contact", "/contact-us", "/about", "/about-us"]
NOISE_DOMAINS        = {
    "example.com","sentry.io","w3.org","schema.org","cloudflare.com",
    "google.com","wixpress.com","wordpress.org","jquery.com",
}

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# EMAIL EXTRACTION
# ─────────────────────────────────────────────

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
HEADERS_BOT = {"User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0; +mailto:you@youragency.com)"}

def extract_emails(url: str) -> set[str]:
    if not url:
        return set()
    found: set[str] = set()
    base = url.rstrip("/")
    for path in [""] + CONTACT_PATHS:
        try:
            r = requests.get(base + path, headers=HEADERS_BOT, timeout=EMAIL_TIMEOUT_SEC)
            if r.status_code == 200:
                for e in EMAIL_REGEX.findall(r.text):
                    domain = e.split("@")[-1].lower()
                    if domain not in NOISE_DOMAINS and "." in domain:
                        found.add(e.lower())
        except Exception:
            pass
    return found

# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────

SHEET_HEADERS = [
    "Business Name", "Category", "Address", "Phone",
    "Website", "Emails Found", "Rating", "Reviews",
    "Google Maps URL", "Scraped At",
]
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def init_sheet():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        log.warning(
            "Google Sheets service account file '%s' not found. "
            "Will fallback to local CSV output.", SERVICE_ACCOUNT_FILE
        )
        return None
    if os.path.getsize(SERVICE_ACCOUNT_FILE) == 0:
        log.warning(
            "Google Sheets service account file '%s' is empty. "
            "Will fallback to local CSV output.", SERVICE_ACCOUNT_FILE
        )
        return None

    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SHEET_SCOPES)
        client = gspread.authorize(creds)
        try:
            sheet = client.open(SPREADSHEET_NAME).sheet1
        except gspread.SpreadsheetNotFound:
            ss = client.create(SPREADSHEET_NAME)
            sheet = ss.sheet1
        if not sheet.row_values(1):
            sheet.append_row(SHEET_HEADERS, value_input_option="RAW")
        return sheet
    except Exception as exc:
        log.warning(
            "Google Sheets initialization failed (%s). Falling back to local CSV output.",
            exc,
        )
        return None


def save_rows_to_csv(rows: list[list], filename: str = "gmaps_leads.csv"):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(SHEET_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to local CSV: %s", len(rows), filename)

# ─────────────────────────────────────────────
# PLAYWRIGHT SCRAPING HELPERS
# ─────────────────────────────────────────────

RESULTS_SELECTOR = 'div[role="feed"] > div > div[jsaction]'
RESULTS_FEED_SELECTOR = 'div[role="feed"]'
PLACE_LINK_SELECTOR = 'a.hfpxzc[href*="/maps/place/"]'

async def safe_text(page: Page, selector: str, default: str = "") -> str:
    """Try to get inner text of a selector; return default on failure."""
    try:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return default

async def safe_attr(page: Page, selector: str, attr: str, default: str = "") -> str:
    try:
        el = await page.query_selector(selector)
        if el:
            val = await el.get_attribute(attr)
            return (val or "").strip()
    except Exception:
        pass
    return default

async def wait_for_any_selector(page: Page, selectors: list[str], timeout_ms: int) -> str | None:
    """Return the first selector that appears within the timeout window."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=1200)
                return selector
            except PWTimeout:
                continue
            except Exception:
                continue
    return None

async def wait_for_maps_search_ready(page: Page, timeout_ms: int = 45_000) -> bool:
    """Wait for the Maps search page to become interactive without relying on network idle."""
    ready_selector = await wait_for_any_selector(
        page,
        [
            'div[role="feed"]',
            'input#searchboxinput',
            'button[aria-label="Back"]',
            'button[aria-label="Directions"]',
            'h1',
        ],
        timeout_ms=timeout_ms,
    )
    return ready_selector is not None

async def wait_for_listing_detail_ready(page: Page, timeout_ms: int = 12_000) -> bool:
    """Wait for a listing detail panel to appear."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            current_url = page.url
            name = await safe_text(page, 'h1')
            has_address = await page.locator('[data-item-id="address"]').count() > 0
            has_phone = await page.locator('[data-item-id*="phone"]').count() > 0
            has_website = await page.locator('a[data-item-id="authority"]').count() > 0
            if (
                ("/maps/place/" in current_url or "/place/" in current_url)
                and name
                and name.lower() != "results"
            ):
                return True
            if name and name.lower() != "results" and (has_address or has_phone or has_website):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return False

def normalize_maps_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = f"https://www.google.com{url}"
    return url.strip()

async def collect_place_urls(page: Page, max_results: int) -> list[str]:
    """
    Scroll the left-panel result list and collect stable Google Maps place URLs.
    """
    urls: list[str] = []
    seen_urls: set[str] = set()
    prev_count = 0
    stall_count = 0

    while True:
        anchors = page.locator(PLACE_LINK_SELECTOR)
        anchor_count = await anchors.count()
        for i in range(anchor_count):
            href = normalize_maps_url(await anchors.nth(i).get_attribute("href") or "")
            if href and href not in seen_urls:
                seen_urls.add(href)
                urls.append(href)
                if len(urls) >= max_results:
                    break

        visible_cards = await page.locator(RESULTS_SELECTOR).count()
        log.info("Visible results: %d | Collected place URLs: %d", visible_cards, len(urls))

        if len(urls) >= max_results:
            break

        if len(urls) == prev_count:
            stall_count += 1
            if stall_count >= 4:
                log.info("No more results loading — end of list.")
                break
        else:
            stall_count = 0

        prev_count = len(urls)
        # Scroll inside the panel
        await page.evaluate(f"""
            const el = document.querySelector('{RESULTS_FEED_SELECTOR}');
            if (el) el.scrollBy(0, 800);
        """)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

    return urls[:max_results]

async def extract_listing_detail(page: Page) -> dict | None:
    """Extract listing details from an already-open Maps place page."""
    # Business name
    name = await safe_text(page, 'h1')
    if not name or name.lower() == "results":
        log.warning("Unexpected detail panel title '%s' — skipping.", name or "")
        return None

    # Category (just below name)
    category = await safe_text(page, 'button[jsaction*="category"]')

    # Address — look for the address button
    address = ""
    addr_els = await page.query_selector_all('[data-item-id="address"]')
    for el in addr_els:
        address = (await el.inner_text()).strip()
        if address:
            break

    # Phone
    phone = ""
    phone_els = await page.query_selector_all('[data-item-id*="phone"]')
    for el in phone_els:
        phone = (await el.inner_text()).strip()
        if phone:
            break

    # Website
    website = ""
    website_el = await page.query_selector('a[data-item-id="authority"]')
    if website_el:
        website = await website_el.get_attribute("href") or ""

    # Rating and reviews
    rating_text = await safe_text(page, 'div[jsaction*="rating"] span[aria-hidden="true"]')
    reviews_text = await safe_text(page, 'div[jsaction*="rating"] span[aria-label*="review"]')
    if not rating_text:
        rating_text = await safe_attr(page, 'div[role="img"][aria-label*="stars"]', "aria-label")
    if not reviews_text:
        reviews_text = await safe_text(page, 'button[jsaction*="pane.reviewChart.moreReviews"]')

    # Current URL (Maps listing URL)
    maps_url = page.url

    return {
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "website": website,
        "rating": rating_text,
        "reviews": reviews_text.replace("(", "").replace(")", "").strip(),
        "maps_url": maps_url,
    }

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def goto_with_retry(
    page: Page,
    url: str,
    retries: int = 2,
    timeout_ms: int = 60000,
    ready_check=None,
    ready_timeout_ms: int | None = None,
):
    """Navigate with retries; returns True when successful, False on failure."""
    for attempt in range(1, retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if ready_check is not None:
                timeout_for_ready = ready_timeout_ms or min(timeout_ms, 45_000)
                if not await ready_check(page, timeout_ms=timeout_for_ready):
                    raise PWTimeout("Google Maps page did not become ready in time.")
            return True
        except Exception as exc:
            log.warning("goto failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                wait_ms = 2000 * attempt
                log.info("Retrying after %dms…", wait_ms)
                await page.wait_for_timeout(wait_ms)
    return False


async def run():
    log.info("=" * 60)
    log.info(f"Google Maps FREE Scraper (Playwright)")
    log.info(f"Query   : {SEARCH_QUERY}")
    log.info(f"Max     : {MAX_RESULTS} results")
    log.info("=" * 60)

    sheet = init_sheet()
    rows: list[list] = []
    seen_names: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        # 1. Open Google Maps and search
        search_url = f"https://www.google.com/maps/search/{SEARCH_QUERY.replace(' ', '+')}"
        log.info(f"Navigating to: {search_url}")
        success = await goto_with_retry(
            page,
            search_url,
            retries=3,
            timeout_ms=90_000,
            ready_check=wait_for_maps_search_ready,
            ready_timeout_ms=45_000,
        )
        if not success:
            log.error("Cannot load search page; aborting scrape.")
            await browser.close()
            return
        await page.wait_for_timeout(2000)

        # Dismiss cookie/consent dialogs if present
        for btn_text in ["Accept all", "Reject all", "I agree", "Accept"]:
            try:
                btn = page.get_by_role("button", name=btn_text)
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # 2. Scroll to load result links
        place_urls = await collect_place_urls(page, MAX_RESULTS)
        total_listings = len(place_urls)
        log.info(f"Collected {total_listings} listing URLs to process.")

        # 3. Process each listing
        for i, place_url in enumerate(place_urls, start=1):
            success = await goto_with_retry(
                page,
                place_url,
                retries=2,
                timeout_ms=60_000,
                ready_check=wait_for_listing_detail_ready,
                ready_timeout_ms=15_000,
            )
            if not success:
                log.warning("Listing %d could not be loaded — skipping.", i)
                continue
            await page.wait_for_timeout(DETAIL_PAUSE_MS)
            detail = await extract_listing_detail(page)
            if not detail or not detail["name"]:
                continue

            name = detail["name"]
            if name in seen_names:
                continue
            seen_names.add(name)

            # 4. Scrape emails from business website
            emails = extract_emails(detail["website"])
            emails_str = ", ".join(sorted(emails))

            row = [
                name,
                detail["category"],
                detail["address"],
                detail["phone"],
                detail["website"],
                emails_str,
                detail["rating"],
                detail["reviews"],
                detail["maps_url"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ]
            rows.append(row)

            log.info(
                f"[{len(rows):03d}] {name[:38]:<38}  "
                f"📞 {detail['phone'] or '—':<18}  "
                f"✉ {emails_str or '—'}"
            )

        await browser.close()

    # 5. Export results
    if rows:
        if sheet:
            sheet.append_rows(rows, value_input_option="RAW")
            log.info(f"\nExported {len(rows)} leads to '{SPREADSHEET_NAME}'")
        else:
            save_rows_to_csv(rows)
    else:
        log.warning("No results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(run())
