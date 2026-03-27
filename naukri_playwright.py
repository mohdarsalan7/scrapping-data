"""
Standalone Naukri scraper using Playwright.

This file is separate from gmaps_playwright.py and writes to its own CSV.

Important:
    Naukri often blocks automated traffic with an Access Denied page.
    This script detects that case and exits cleanly with guidance.

Install:
    pip install playwright
    playwright install chromium

Run:
    python naukri_playwright.py
"""

import asyncio
import csv
import logging
import os
import re
from datetime import datetime

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright


# Configuration
SEARCH_QUERY = "python developer"
LOCATION = "delhi"
MAX_RESULTS = 50
HEADLESS = True
PAGE_PAUSE_MS = 2500
SCROLL_PAUSE_MS = 1600

OUTPUT_CSV = "naukri_jobs.csv"
LOG_FILE = "naukri_scraper.log"

CARD_SELECTOR = ".srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple"
TITLE_SELECTOR = "a.title, a.titleFw500"
COMPANY_SELECTOR = ".comp-name, a.comp-name"
LOCATION_SELECTOR = ".locWdth, .row2 span.locWdth"
EXPERIENCE_SELECTOR = ".expwdth, .row2 span.expwdth"
SALARY_SELECTOR = ".sal-wrap, .row2 span.sal-wrap"
DESCRIPTION_SELECTOR = ".job-desc, .job-description, .jobDescriptionContainer"
SKILLS_SELECTOR = ".tags-gt li, .tag-li"
POSTED_SELECTOR = ".job-post-day, .footer .fleft, .jobTupleFooter span"


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
    "Experience",
    "Salary",
    "Posted",
    "Description",
    "Skills",
    "Job URL",
    "Search Query",
    "Search Location",
    "Scraped At",
]


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "jobs"


def build_search_url() -> str:
    return f"https://www.naukri.com/{slugify(SEARCH_QUERY)}-jobs-in-{slugify(LOCATION)}"


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"https://www.naukri.com{url}"
    return url


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


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


async def is_access_denied(page: Page) -> bool:
    title = clean_text(await page.title())
    body = clean_text(await page.locator("body").inner_text())
    return "access denied" in title.lower() or "you don't have permission" in body.lower()


async def wait_for_results(page: Page, timeout_ms: int = 30000) -> bool:
    try:
        await page.wait_for_selector(CARD_SELECTOR, timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


async def collect_visible_cards(page: Page) -> int:
    prev_count = 0
    stall_count = 0
    while True:
        count = await page.locator(CARD_SELECTOR).count()
        log.info("Visible Naukri cards: %d", count)
        if count >= MAX_RESULTS:
            return count
        if count == prev_count:
            stall_count += 1
            if stall_count >= 4:
                return count
        else:
            stall_count = 0
        prev_count = count
        await page.evaluate("window.scrollBy(0, 1400)")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)


async def extract_card(card) -> dict | None:
    title = await safe_text(card, TITLE_SELECTOR)
    company = await safe_text(card, COMPANY_SELECTOR)
    location = await safe_text(card, LOCATION_SELECTOR)
    experience = await safe_text(card, EXPERIENCE_SELECTOR)
    salary = await safe_text(card, SALARY_SELECTOR)
    posted = await safe_text(card, POSTED_SELECTOR)
    description = await safe_text(card, DESCRIPTION_SELECTOR)
    job_url = normalize_url(await safe_attr(card, TITLE_SELECTOR, "href"))

    skills = []
    skill_nodes = card.locator(SKILLS_SELECTOR)
    for i in range(await skill_nodes.count()):
        skill = await safe_text(skill_nodes.nth(i), ":scope")
        if skill:
            skills.append(skill)

    if not title and not company:
        return None

    return {
        "title": title,
        "company": company,
        "location": location,
        "experience": experience,
        "salary": salary,
        "posted": posted,
        "description": description,
        "skills": ", ".join(skills),
        "job_url": job_url,
    }


def save_rows(rows: list[list], filename: str = OUTPUT_CSV):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(CSV_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to %s", len(rows), filename)


async def run():
    search_url = build_search_url()

    log.info("=" * 60)
    log.info("Naukri Scraper")
    log.info("Query   : %s", SEARCH_QUERY)
    log.info("Location: %s", LOCATION)
    log.info("Max     : %d results", MAX_RESULTS)
    log.info("URL     : %s", search_url)
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

        log.info("Loading: %s", search_url)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_PAUSE_MS)

        if await is_access_denied(page):
            log.error("Naukri returned Access Denied from this IP/session.")
            log.error("This is an anti-bot block, not a parser error.")
            log.error("Try HEADLESS = False on a local desktop session or a different network/profile.")
            await browser.close()
            return

        if not await wait_for_results(page):
            log.error("Naukri results did not appear. The site may have changed its layout or blocked the request.")
            await browser.close()
            return

        await collect_visible_cards(page)
        cards = page.locator(CARD_SELECTOR)
        count = min(await cards.count(), MAX_RESULTS)

        for i in range(count):
            item = await extract_card(cards.nth(i))
            if not item:
                continue

            key = item["job_url"] or f'{item["title"]}|{item["company"]}|{item["location"]}'
            if key in seen_urls:
                continue
            seen_urls.add(key)

            rows.append([
                item["title"],
                item["company"],
                item["location"],
                item["experience"],
                item["salary"],
                item["posted"],
                item["description"],
                item["skills"],
                item["job_url"],
                SEARCH_QUERY,
                LOCATION,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ])

            log.info(
                "[%03d] %s | %s | %s",
                len(rows),
                item["title"] or "-",
                item["company"] or "-",
                item["location"] or "-",
            )

        await browser.close()

    if rows:
        save_rows(rows)
    else:
        log.warning("No Naukri results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
