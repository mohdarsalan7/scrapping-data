"""
Standalone Indeed scraper using Playwright.

This file is separate from gmaps_playwright.py and writes to its own CSV.

Install:
    pip install playwright requests
    playwright install chromium

Run:
    python indeed_playwright.py
"""

import asyncio
import csv
import logging
import os
import re
from datetime import datetime
from urllib.parse import urlencode

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright


# Configuration
SEARCH_QUERY = "MERN developer"
LOCATION = "delhi"
MAX_RESULTS = 50
RESULTS_PER_PAGE = 10
HEADLESS = True
PAGE_PAUSE_MS = 2000
SEARCH_READY_TIMEOUT_MS = 45_000
NAVIGATION_RETRIES = 3
RETRY_BACKOFF_MS = 5000

OUTPUT_CSV = "indeed_jobs.csv"
LOG_FILE = "indeed_scraper.log"

INDEED_BASE = "https://in.indeed.com"
CARD_SELECTOR = '[data-testid="slider_item"]'
SIGNIN_TITLE_FRAGMENT = "Sign In | Indeed Accounts"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


CSV_HEADERS = [
    "Job Title",
    "Company",
    "Location",
    "Salary",
    "Job Type",
    "Posted",
    "Summary",
    "Job URL",
    "Search Query",
    "Search Location",
    "Scraped At",
]


def build_search_url(start: int = 0) -> str:
    params = {"q": SEARCH_QUERY, "l": LOCATION}
    if start:
        params["start"] = str(start)
    return f"{INDEED_BASE}/jobs?{urlencode(params)}"


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        return f"{INDEED_BASE}{url}"
    return url


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def split_lines(text: str) -> list[str]:
    return [clean_text(line) for line in (text or "").splitlines() if clean_text(line)]


async def safe_text(scope, selector: str, default: str = "") -> str:
    try:
        loc = scope.locator(selector).first
        if await loc.count():
            return clean_text(await loc.inner_text())
    except Exception:
        pass
    return default


async def safe_attr(scope, selector: str, attr: str, default: str = "") -> str:
    try:
        loc = scope.locator(selector).first
        if await loc.count():
            return clean_text(await loc.get_attribute(attr) or "")
    except Exception:
        pass
    return default


async def wait_for_search_ready(page: Page, timeout_ms: int = 30000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await page.locator(CARD_SELECTOR).count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def is_signin_page(page: Page) -> bool:
    title = clean_text(await page.title())
    body = clean_text(await page.locator("body").inner_text())
    return SIGNIN_TITLE_FRAGMENT in title or "to see more than one page of jobs" in body.lower()


async def is_challenge_page(page: Page) -> bool:
    title = clean_text(await page.title()).lower()
    body = clean_text(await page.locator("body").inner_text()).lower()
    patterns = [
        "just a moment",
        "additional verification required",
        "verify you are human",
        "security check",
        "unusual traffic",
        "captcha",
    ]
    return any(pattern in title or pattern in body for pattern in patterns)


def split_metadata(items: list[str]) -> tuple[str, str]:
    salary = ""
    job_bits: list[str] = []
    for item in items:
        if not item:
            continue
        normalized = clean_text(item)
        if not normalized:
            continue
        if not salary and any(token in normalized for token in ["₹", "$", "year", "month", "hour", "week"]):
            salary = normalized
        else:
            job_bits.append(normalized)
    return salary, " | ".join(job_bits)


def extract_posted_from_lines(lines: list[str]) -> str:
    patterns = [
        r"\b\d+\+?\s+(?:day|days|hour|hours|minute|minutes)\s+ago\b",
        r"\bjust posted\b",
        r"\btoday\b",
        r"\bposted\b.*",
        r"\bactive\b.*",
    ]
    for line in lines:
        lower = line.lower()
        for pattern in patterns:
            match = re.search(pattern, lower, flags=re.IGNORECASE)
            if match:
                return clean_text(line)
    return ""


def parse_card_fallback(lines: list[str], title: str, company: str, location: str) -> tuple[str, str, str]:
    salary = ""
    job_type = ""
    posted = ""
    residual: list[str] = []
    job_type_tokens = [
        "full-time", "part-time", "permanent", "contract", "internship",
        "temporary", "fresher", "freelance", "shift",
    ]

    ignored = {
        clean_text(title).lower(),
        clean_text(company).lower(),
        clean_text(location).lower(),
        "easily apply",
        "new",
    }

    for line in lines:
        lowered = line.lower()
        if lowered in ignored:
            continue
        if not salary and any(token in line for token in ["₹", "$", "year", "month", "week", "hour"]):
            salary = line
            continue
        if not posted and extract_posted_from_lines([line]):
            posted = line
            continue
        if not job_type and any(token in lowered for token in job_type_tokens):
            job_type = line
            continue
        residual.append(line)

    return salary, job_type, posted, " | ".join(residual)


async def extract_card(card) -> dict | None:
    raw_card_text = await card.inner_text()
    lines = split_lines(raw_card_text)
    title = await safe_attr(card, 'h2 a span[title], h2 span[title]', "title")
    if not title:
        title = await safe_text(card, "h2")
    company = await safe_text(card, '[data-testid="company-name"]')
    location = await safe_text(card, '[data-testid="text-location"]')
    posted = await safe_text(card, '[data-testid="myJobsStateDate"], .date')
    summary = await safe_text(card, '.job-snippet, [data-testid="text-snippet"]')

    href = await safe_attr(card, 'h2 a[href]', "href")
    job_url = normalize_url(href)

    meta_items = []
    meta_locs = card.locator('li[data-testid*="attribute_snippet_testid"]')
    for i in range(await meta_locs.count()):
        try:
            meta_items.append(clean_text(await meta_locs.nth(i).inner_text()))
        except Exception:
            continue
    salary, job_type = split_metadata(meta_items)
    fallback_salary, fallback_job_type, fallback_posted, fallback_summary = parse_card_fallback(
        lines,
        title=title,
        company=company,
        location=location,
    )

    if not salary:
        salary = fallback_salary
    if not job_type:
        job_type = fallback_job_type
    if not posted:
        posted = fallback_posted
    if not summary:
        summary = fallback_summary

    if not title and not company:
        return None

    return {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
        "job_type": job_type,
        "posted": posted,
        "summary": summary,
        "job_url": job_url,
    }


async def scrape_search_page(page: Page, url: str) -> list[dict]:
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        log.info("Loading: %s (attempt %d/%d)", url, attempt, NAVIGATION_RETRIES)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_PAUSE_MS)

        if await is_signin_page(page):
            log.warning("Indeed redirected this page to sign-in: %s", page.url)
            log.warning("Indeed is limiting pagination for anonymous scraping from this session.")
            return []

        if await is_challenge_page(page):
            log.warning("Indeed returned a verification/interstitial page on attempt %d.", attempt)
        elif await wait_for_search_ready(page, timeout_ms=SEARCH_READY_TIMEOUT_MS):
            cards = page.locator(CARD_SELECTOR)
            results: list[dict] = []
            count = await cards.count()
            log.info("Visible Indeed cards: %d", count)
            for i in range(count):
                item = await extract_card(cards.nth(i))
                if item:
                    results.append(item)
            return results
        else:
            log.warning("Indeed results did not appear on attempt %d for: %s", attempt, url)

        if attempt < NAVIGATION_RETRIES:
            wait_ms = RETRY_BACKOFF_MS * attempt
            log.info("Retrying after %dms…", wait_ms)
            await page.wait_for_timeout(wait_ms)

    return []


def save_rows(rows: list[list], filename: str = OUTPUT_CSV):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(CSV_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to %s", len(rows), filename)


async def run():
    log.info("=" * 60)
    log.info("Indeed Scraper")
    log.info("Query   : %s", SEARCH_QUERY)
    log.info("Location: %s", LOCATION)
    log.info("Max     : %d results", MAX_RESULTS)
    log.info("=" * 60)

    rows: list[list] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        for start in range(0, MAX_RESULTS, RESULTS_PER_PAGE):
            page_rows = await scrape_search_page(page, build_search_url(start=start))
            if not page_rows:
                break

            new_rows = 0
            for item in page_rows:
                job_url = item["job_url"]
                key = job_url or f'{item["title"]}|{item["company"]}|{item["location"]}'
                if key in seen_urls:
                    continue
                seen_urls.add(key)

                rows.append([
                    item["title"],
                    item["company"],
                    item["location"],
                    item["salary"],
                    item["job_type"],
                    item["posted"],
                    item["summary"],
                    job_url,
                    SEARCH_QUERY,
                    LOCATION,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ])
                new_rows += 1

                log.info(
                    "[%03d] %s | %s | %s",
                    len(rows),
                    item["title"] or "-",
                    item["company"] or "-",
                    item["location"] or "-",
                )
                if len(rows) >= MAX_RESULTS:
                    break

            if new_rows == 0 or len(rows) >= MAX_RESULTS:
                break

        await browser.close()

    if rows:
        save_rows(rows[:MAX_RESULTS])
    else:
        log.warning("No Indeed results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
