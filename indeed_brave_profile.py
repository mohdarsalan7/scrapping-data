"""
Standalone Indeed scraper using your signed-in Brave profile.

This file does not modify the existing project scripts.

Important:
    1. Close Brave completely before running this script.
    2. This script launches Brave in headful mode using your real profile.
    3. If Indeed shows a verification page, solve it in the opened browser window.

Run:
    python indeed_brave_profile.py
"""

import asyncio
import csv
import logging
import os
import re
import shutil
from datetime import datetime
from urllib.parse import urlencode

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright


# Configuration
SEARCH_QUERY = "MERN developer"
LOCATION = "Gurugram"
MAX_RESULTS = 50
RESULTS_PER_PAGE = 10

# Brave profile settings
BRAVE_EXECUTABLE = shutil.which("brave-browser") or "/usr/bin/brave-browser"
BRAVE_USER_DATA_DIR = os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")
BRAVE_PROFILE_DIRECTORY = "Default"
HEADLESS = False

# Scraper behavior
PAGE_PAUSE_MS = 2500
SEARCH_READY_TIMEOUT_MS = 60_000
NAVIGATION_RETRIES = 3
RETRY_BACKOFF_MS = 5000
MANUAL_RECOVERY_WAIT_MS = 20_000

OUTPUT_CSV = "indeed_brave_profile_jobs.csv"
LOG_FILE = "indeed_brave_profile.log"

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
    "Job Description",
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


async def wait_for_search_ready(page: Page, timeout_ms: int = SEARCH_READY_TIMEOUT_MS) -> bool:
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


async def wait_for_manual_recovery(page: Page, reason: str, wait_ms: int = MANUAL_RECOVERY_WAIT_MS):
    log.warning("%s detected. Complete the step in the open Brave window.", reason)
    log.warning("Waiting %d seconds for manual recovery...", wait_ms // 1000)
    await page.wait_for_timeout(wait_ms)


def split_metadata(items: list[str]) -> tuple[str, str]:
    salary = ""
    job_bits: list[str] = []
    for item in items:
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
        for pattern in patterns:
            if re.search(pattern, line.lower(), flags=re.IGNORECASE):
                return clean_text(line)
    return ""


def parse_card_fallback(lines: list[str], title: str, company: str, location: str) -> tuple[str, str, str, str]:
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
        "job_description": "",
        "job_url": job_url,
    }


async def wait_for_job_detail(page: Page, timeout_ms: int = 20_000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            description = page.locator("#jobDescriptionText")
            title = page.locator('[data-testid="jobsearch-JobInfoHeader-title"]')
            if await description.count() > 0 and await title.count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(800)
    return False


async def enrich_with_job_detail(page: Page, index: int, item: dict) -> dict:
    cards = page.locator(CARD_SELECTOR)
    count = await cards.count()
    if index >= count:
        return item

    card = cards.nth(index)
    try:
        await card.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)
        await card.click()
        if not await wait_for_job_detail(page):
            return item
    except Exception as exc:
        log.warning("Could not open Indeed detail for row %d: %s", index + 1, exc)
        return item

    detail_title = await safe_text(page, '[data-testid="jobsearch-JobInfoHeader-title"]')
    detail_company = await safe_text(page, '[data-testid="inlineHeader-companyName"]')
    detail_location = await safe_text(page, '[data-testid="inlineHeader-companyLocation"]')
    detail_description = await safe_text(page, '#jobDescriptionText')
    detail_meta = await safe_text(page, '#salaryInfoAndJobType')
    detail_url = page.url

    if detail_title:
        item["title"] = detail_title.replace(" - job post", "").strip()
    if detail_company:
        item["company"] = detail_company
    if detail_location:
        item["location"] = detail_location
    if detail_url:
        item["job_url"] = detail_url
    if detail_description:
        item["job_description"] = detail_description

    if detail_meta:
        lines = split_lines(detail_meta)
        if not item["salary"]:
            item["salary"] = next(
                (line for line in lines if any(token in line for token in ["₹", "$", "year", "month", "week", "hour"])),
                item["salary"],
            )
        if not item["job_type"]:
            item["job_type"] = " | ".join(
                line for line in lines
                if not any(token in line for token in ["₹", "$", "year", "month", "week", "hour"])
            )

    return item


async def scrape_search_page(page: Page, url: str) -> list[dict]:
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        log.info("Loading: %s (attempt %d/%d)", url, attempt, NAVIGATION_RETRIES)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_PAUSE_MS)

        if await is_signin_page(page):
            log.warning("Indeed redirected to sign-in: %s", page.url)
            if not HEADLESS:
                await wait_for_manual_recovery(page, "Indeed sign-in page")
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(PAGE_PAUSE_MS)
                if await is_signin_page(page):
                    log.warning("Still on sign-in after manual wait.")
                    return []
            else:
                return []

        if await is_challenge_page(page):
            if not HEADLESS:
                await wait_for_manual_recovery(page, "Indeed verification page")
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(PAGE_PAUSE_MS)
            else:
                log.warning("Indeed verification page detected.")

        if await wait_for_search_ready(page):
            cards = page.locator(CARD_SELECTOR)
            results: list[dict] = []
            count = await cards.count()
            log.info("Visible Indeed cards: %d", count)
            for i in range(count):
                item = await extract_card(cards.nth(i))
                if item:
                    item = await enrich_with_job_detail(page, i, item)
                    results.append(item)
            return results

        if attempt < NAVIGATION_RETRIES:
            wait_ms = RETRY_BACKOFF_MS * attempt
            log.info("Retrying after %dms…", wait_ms)
            await page.wait_for_timeout(wait_ms)

    log.warning("Indeed results did not appear for: %s", url)
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
    if not os.path.exists(BRAVE_USER_DATA_DIR):
        raise FileNotFoundError(f"Brave user data dir not found: {BRAVE_USER_DATA_DIR}")
    if not os.path.exists(BRAVE_EXECUTABLE):
        raise FileNotFoundError(f"Brave executable not found: {BRAVE_EXECUTABLE}")

    log.info("=" * 60)
    log.info("Indeed Brave Profile Scraper")
    log.info("Query   : %s", SEARCH_QUERY)
    log.info("Location: %s", LOCATION)
    log.info("Max     : %d results", MAX_RESULTS)
    log.info("Brave   : %s", BRAVE_EXECUTABLE)
    log.info("Profile : %s (%s)", BRAVE_PROFILE_DIRECTORY, BRAVE_USER_DATA_DIR)
    log.info("=" * 60)
    log.info("Close Brave fully before running this script to avoid profile lock issues.")

    rows: list[list] = []
    seen_urls: set[str] = set()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=BRAVE_USER_DATA_DIR,
            executable_path=BRAVE_EXECUTABLE,
            headless=HEADLESS,
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
            args=[
                f"--profile-directory={BRAVE_PROFILE_DIRECTORY}",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()

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
                    item["job_description"],
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

        await context.close()

    if rows:
        save_rows(rows[:MAX_RESULTS])
    else:
        log.warning("No Indeed results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
