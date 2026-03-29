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
import html
import logging
import asyncio
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import gspread
import requests
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ─────────────────────────────────────────────
# CONFIGURATION — edit before running
# ─────────────────────────────────────────────

SEARCH_QUERY        = "Garment Stores in Delhi"  # Single-query fallback
SEARCH_QUERIES      = [
    SEARCH_QUERY,
    # "Clothing Store in Delhi",
    # "Readymade Garments in Delhi",
    # "Boutique in Delhi",
]
MAX_RESULTS         = 1000     # How many listings to scrape (scroll until reached)
SCROLL_PAUSE_MS     = 1800         # Pause between scrolls (ms) — be polite
DETAIL_PAUSE_MS     = 1200         # Pause before reading each listing detail
HEADLESS            = True         # False = see the browser (useful for debugging)
CONTINUOUS_SAVE     = True         # Save in batches while scraping instead of only at the end
SAVE_EVERY_N_ROWS   = 1            # Flush pending rows after this many new leads

SERVICE_ACCOUNT_FILE = "service_account.json"   # For writing to Google Sheets
SPREADSHEET_NAME     = "GMaps Leads (Free)"
OUTPUT_CSV           = "gmaps_leads.csv"

# Email scraping
EMAIL_TIMEOUT_SEC    = 8
CONTACT_PATHS        = ["/contact", "/contact-us", "/about", "/about-us"]
DEFAULT_PHONE_COUNTRY_CODE = "+91"
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
# CONTACT EXTRACTION
# ─────────────────────────────────────────────

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"(?:(?<=\D)|^)(\+?\d[\d\s().-]{7,}\d)(?=\D|$)")
MAILTO_REGEX = re.compile(r'(?i)mailto:([^"\'<>\s?#]+)')
TEL_REGEX = re.compile(r'(?i)tel:([^"\'<>\s?#]+)')
HEADERS_BOT = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"}
BLOCKED_EMAIL_LOCALPARTS = {
    "abuse", "admin", "billing", "hello", "help", "legal", "mailer-daemon",
    "marketing", "news", "newsletter", "no-reply", "nobody", "noreply",
    "notifications", "postmaster", "privacy", "security", "support",
    "unsubscribe", "updates",
}
BLOCKED_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "ico",
    "css", "js", "json", "xml", "map", "txt", "woff", "woff2", "ttf", "otf",
}
CONTACT_HINTS = [
    "call", "contact", "customer care", "email", "mobile", "phone",
    "reach us", "support", "tel", "whatsapp",
]
COMMON_INDIA_STD_CODES_2 = {"11", "20", "22", "33", "40", "44", "79", "80"}
COMMON_INDIA_STD_CODES_3 = {"120", "124", "129", "135", "141", "172", "175", "183"}


def normalize_business_website(url: str) -> str:
    if not url:
        return ""

    url = url.strip()
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path.startswith("/url"):
        target = parse_qs(parsed.query).get("q", [""])[0]
        if target:
            url = target
            parsed = urlparse(url)

    if url.startswith("//"):
        url = f"https:{url}"
        parsed = urlparse(url)

    if not parsed.scheme:
        url = f"https://{url}"

    return url.rstrip("/")


def normalize_email(email: str) -> str:
    return email.strip(".,;:()[]{}<>\"'").lower()


def looks_like_india_phone_digits(digits: str) -> bool:
    if digits.startswith("1800") and len(digits) == 11:
        return True
    if len(digits) == 10:
        return (
            digits[0] in "6789"
            or digits[:2] in COMMON_INDIA_STD_CODES_2
            or digits[:3] in COMMON_INDIA_STD_CODES_3
        )
    if len(digits) == 11 and digits.startswith("0"):
        return looks_like_india_phone_digits(digits[1:])
    if len(digits) == 12 and digits.startswith("91"):
        return looks_like_india_phone_digits(digits[2:])
    return False


def normalize_phone_number(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if not digits:
        return ""

    if not looks_like_india_phone_digits(digits):
        return ""

    if raw_phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1800"):
        return digits
    if len(digits) == 10:
        return f"{DEFAULT_PHONE_COUNTRY_CODE}{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"{DEFAULT_PHONE_COUNTRY_CODE}{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return ""


def is_valid_email(email: str) -> bool:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return False
    local_part, domain = normalized.split("@", 1)
    tld = domain.rsplit(".", 1)[-1].lower()
    if local_part in BLOCKED_EMAIL_LOCALPARTS:
        return False
    if tld in BLOCKED_EMAIL_TLDS:
        return False
    if domain in NOISE_DOMAINS or "." not in domain:
        return False
    return True


def is_valid_phone(raw_phone: str) -> bool:
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) < 10 or len(digits) > 12:
        return False
    if len(set(digits)) == 1:
        return False
    return looks_like_india_phone_digits(digits)


def clean_visible_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript|svg|canvas|iframe).*?>.*?</\1>", " ", raw_html)
    cleaned = re.sub(r"(?is)<!--.*?-->", " ", cleaned)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|li|section|article|footer|header|h[1-6]|tr|td)>", "\n", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    cleaned = re.sub(r"\n+", "\n", cleaned)
    return cleaned


def has_contact_hint(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in CONTACT_HINTS)


def extract_contacts_from_visible_text(text: str) -> tuple[set[str], set[str]]:
    emails: set[str] = set()
    phones: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 220:
            continue

        line_has_hint = has_contact_hint(line)

        for email in EMAIL_REGEX.findall(line):
            normalized_email = normalize_email(email)
            if not is_valid_email(normalized_email):
                continue
            if line_has_hint or len(line) <= 120:
                emails.add(normalized_email)

        for raw_phone in PHONE_REGEX.findall(line):
            if not is_valid_phone(raw_phone):
                continue
            if not line_has_hint and len(line) > 80:
                continue
            normalized_phone = normalize_phone_number(raw_phone)
            if normalized_phone:
                phones.add(normalized_phone)

    return emails, phones


def extract_site_contacts(url: str) -> tuple[set[str], set[str]]:
    if not url:
        return set(), set()

    emails: set[str] = set()
    phones: set[str] = set()
    base_url = normalize_business_website(url)
    if not base_url:
        return emails, phones

    session = requests.Session()
    visited: set[str] = set()

    for path in [""] + CONTACT_PATHS:
        target_url = urljoin(base_url + "/", path.lstrip("/")) if path else base_url
        if target_url in visited:
            continue
        visited.add(target_url)
        try:
            response = session.get(target_url, headers=HEADERS_BOT, timeout=EMAIL_TIMEOUT_SEC)
            if response.status_code != 200:
                continue

            content_type = (response.headers.get("content-type") or "").lower()
            if "html" not in content_type and "text" not in content_type:
                continue

            raw_html = response.text

            for match in MAILTO_REGEX.findall(raw_html):
                normalized_email = normalize_email(unquote(match).split("?", 1)[0])
                if is_valid_email(normalized_email):
                    emails.add(normalized_email)

            for match in TEL_REGEX.findall(raw_html):
                raw_phone = unquote(match).split("?", 1)[0]
                if not is_valid_phone(raw_phone):
                    continue
                normalized_phone = normalize_phone_number(raw_phone)
                if normalized_phone:
                    phones.add(normalized_phone)

            visible_text = clean_visible_text(raw_html)
            visible_emails, visible_phones = extract_contacts_from_visible_text(visible_text)
            emails.update(visible_emails)
            phones.update(visible_phones)
        except Exception:
            continue

    return emails, phones


def normalize_key_part(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def build_lead_key(name: str, address: str, maps_url: str, website: str) -> str:
    normalized_maps_url = normalize_maps_url(maps_url)
    if normalized_maps_url:
        return f"maps::{normalized_maps_url}"
    return "meta::" + "|".join(
        [
            normalize_key_part(name),
            normalize_key_part(address),
            normalize_key_part(website),
        ]
    )


def build_search_queries() -> list[str]:
    candidates = [query.strip() for query in SEARCH_QUERIES if query.strip()]
    if not candidates and SEARCH_QUERY.strip():
        candidates = [SEARCH_QUERY.strip()]

    unique_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in candidates:
        normalized = query.lower()
        if normalized in seen_queries:
            continue
        seen_queries.add(normalized)
        unique_queries.append(query)
    return unique_queries

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


def save_rows_to_csv(rows: list[list], filename: str = OUTPUT_CSV):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(SHEET_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to local CSV: %s", len(rows), filename)


def load_existing_keys_from_csv(filename: str = OUTPUT_CSV) -> set[str]:
    if not os.path.exists(filename):
        return set()

    keys: set[str] = set()
    try:
        with open(filename, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                keys.add(
                    build_lead_key(
                        row.get("Business Name", ""),
                        row.get("Address", ""),
                        row.get("Google Maps URL", ""),
                        row.get("Website", ""),
                    )
                )
    except Exception as exc:
        log.warning("Could not load existing CSV keys from %s: %s", filename, exc)
    return keys


def load_existing_keys_from_sheet(sheet) -> set[str]:
    keys: set[str] = set()
    if sheet is None:
        return keys

    try:
        rows = sheet.get_all_records()
        for row in rows:
            keys.add(
                build_lead_key(
                    row.get("Business Name", ""),
                    row.get("Address", ""),
                    row.get("Google Maps URL", ""),
                    row.get("Website", ""),
                )
            )
    except Exception as exc:
        log.warning("Could not load existing Google Sheet keys: %s", exc)
    return keys


def flush_pending_rows(
    pending_rows: list[list],
    sheet=None,
    filename: str = OUTPUT_CSV,
):
    if not pending_rows:
        return

    rows_to_write = pending_rows[:]
    if sheet:
        try:
            sheet.append_rows(rows_to_write, value_input_option="RAW")
            log.info("Saved %d rows to Google Sheets: %s", len(rows_to_write), SPREADSHEET_NAME)
        except Exception as exc:
            log.warning("Google Sheets append failed (%s). Falling back to local CSV output.", exc)
            save_rows_to_csv(rows_to_write, filename=filename)
    else:
        save_rows_to_csv(rows_to_write, filename=filename)

    pending_rows.clear()

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
        website = normalize_business_website(await website_el.get_attribute("href") or "")

    # Rating and reviews are optional capture fields only; they are not used to filter leads.
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
    search_queries = build_search_queries()
    log.info("=" * 60)
    log.info(f"Google Maps FREE Scraper (Playwright)")
    log.info(f"Queries : {' | '.join(search_queries)}")
    log.info(f"Max     : {MAX_RESULTS} results")
    log.info("=" * 60)

    sheet = init_sheet()
    rows: list[list] = []
    pending_rows: list[list] = []
    seen_keys = load_existing_keys_from_csv(OUTPUT_CSV)
    if sheet:
        seen_keys |= load_existing_keys_from_sheet(sheet)
    seen_place_urls: set[str] = set()
    log.info("Existing lead keys loaded: %d", len(seen_keys))

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

        for query_index, search_query in enumerate(search_queries, start=1):
            if len(rows) >= MAX_RESULTS:
                break

            search_url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"
            log.info("Starting query %d/%d: %s", query_index, len(search_queries), search_query)
            log.info("Navigating to: %s", search_url)
            success = await goto_with_retry(
                page,
                search_url,
                retries=3,
                timeout_ms=90_000,
                ready_check=wait_for_maps_search_ready,
                ready_timeout_ms=45_000,
            )
            if not success:
                log.warning("Could not load query: %s", search_query)
                continue
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

            place_urls = await collect_place_urls(page, MAX_RESULTS)
            total_listings = len(place_urls)
            log.info("Collected %d listing URLs for query '%s'.", total_listings, search_query)

            for i, place_url in enumerate(place_urls, start=1):
                if len(rows) >= MAX_RESULTS:
                    break

                normalized_place_url = normalize_maps_url(place_url)
                if normalized_place_url in seen_place_urls:
                    continue
                seen_place_urls.add(normalized_place_url)

                success = await goto_with_retry(
                    page,
                    place_url,
                    retries=2,
                    timeout_ms=60_000,
                    ready_check=wait_for_listing_detail_ready,
                    ready_timeout_ms=15_000,
                )
                if not success:
                    log.warning("Listing %d for query '%s' could not be loaded — skipping.", i, search_query)
                    continue
                await page.wait_for_timeout(DETAIL_PAUSE_MS)
                detail = await extract_listing_detail(page)
                if not detail or not detail["name"]:
                    continue

                lead_key = build_lead_key(
                    detail["name"],
                    detail["address"],
                    detail["maps_url"],
                    detail["website"],
                )
                if lead_key in seen_keys:
                    continue
                seen_keys.add(lead_key)

                name = detail["name"]

                # 4. Scrape business website contacts
                website_emails, website_phones = extract_site_contacts(detail["website"])
                emails_str = ", ".join(sorted(website_emails))

                phones: list[str] = []
                primary_phone = normalize_phone_number(detail["phone"]) if detail["phone"] else ""
                if primary_phone:
                    phones.append(primary_phone)
                for website_phone in sorted(website_phones):
                    if website_phone not in phones:
                        phones.append(website_phone)
                phone_str = ", ".join(phones)

                row = [
                    name,
                    detail["category"],
                    detail["address"],
                    phone_str,
                    detail["website"],
                    emails_str,
                    detail["rating"],
                    detail["reviews"],
                    detail["maps_url"],
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ]
                rows.append(row)
                pending_rows.append(row)

                log.info(
                    f"[{len(rows):03d}] {name[:38]:<38}  "
                    f"📞 {phone_str or '—':<18}  "
                    f"✉ {emails_str or '—'}  "
                    f"query={search_query}"
                )
                if CONTINUOUS_SAVE and len(pending_rows) >= SAVE_EVERY_N_ROWS:
                    flush_pending_rows(pending_rows, sheet=sheet, filename=OUTPUT_CSV)

        await browser.close()

    # 5. Export results
    if rows:
        if pending_rows:
            flush_pending_rows(pending_rows, sheet=sheet, filename=OUTPUT_CSV)
        log.info("\nExported %d new leads", len(rows))
    else:
        log.warning("No results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(run())
