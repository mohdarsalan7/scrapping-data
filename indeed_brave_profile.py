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
import subprocess
from datetime import datetime
from urllib.parse import urlencode

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright


# Configuration
SEARCH_QUERY = "MERN developer"
LOCATION = "Gurugram"
MAX_RESULTS = 500
RESULTS_PER_PAGE = 10
SEARCH_TERMS_OVERRIDE: list[str] = []
AUTO_EXPAND_RELATED_SEARCHES = True
RESUME_PDF = "Mohd_Arsalan.pdf"
USE_RESUME_MATCHING = True
RESUME_SEARCH_TERMS_LIMIT = 10
MIN_PROFILE_MATCH_SCORE = 4

# Brave profile settings
BRAVE_EXECUTABLE = shutil.which("brave-browser") or "/usr/bin/brave-browser"
BRAVE_USER_DATA_DIR = os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")
BRAVE_PROFILE_DIRECTORY = "Default"
HEADLESS = False
USE_PROFILE_CLONE = True
PROFILE_CLONE_ROOT = "/tmp/indeed_brave_profile_clone"

# Scraper behavior
PAGE_PAUSE_MS = 2500
SEARCH_READY_TIMEOUT_MS = 60_000
NAVIGATION_RETRIES = 3
RETRY_BACKOFF_MS = 5000
MANUAL_RECOVERY_WAIT_MS = 20_000
DETAIL_BATCH_SIZE = 4
CONTINUOUS_SAVE = True
SAVE_EVERY_N_ROWS = 1

OUTPUT_CSV = "indeed_brave_profile_jobs.csv"
LOG_FILE = "indeed_brave_profile.log"

INDEED_BASE = "https://in.indeed.com"
CARD_SELECTOR = '[data-testid="slider_item"]'
SIGNIN_TITLE_FRAGMENT = "Sign In | Indeed Accounts"

CONTACT_DEFAULT_COUNTRY_CODE = "+91"
CONTACT_CONTEXT_WINDOW = 120

PUBLIC_CONTACT_HINTS = [
    "apply at",
    "apply on",
    "call",
    "contact",
    "drop your cv",
    "drop your resume",
    "email",
    "mail us",
    "reach us",
    "send cv",
    "send resume",
    "share cv",
    "share profile",
    "share resume",
    "whatsapp",
]

BLOCKED_CONTACT_CONTEXT_PATTERNS = [
    "do not call",
    "don't call",
    "no call",
    "no calls",
    "no cold calling",
    "please do not call",
]

BLOCKED_EMAIL_LOCALPARTS = {
    "abuse",
    "admin",
    "billing",
    "compliance",
    "dmca",
    "donotreply",
    "help",
    "hello",
    "legal",
    "mailer-daemon",
    "marketing",
    "news",
    "newsletter",
    "no-reply",
    "nobody",
    "noreply",
    "notifications",
    "postmaster",
    "privacy",
    "security",
    "support",
    "unsubscribe",
    "updates",
}

EMAIL_REGEX = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_CANDIDATE_REGEX = re.compile(
    r"(?:(?<=\D)|^)(\+?\d[\d\s().-]{7,}\d)(?=\D|$)"
)


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
    "Public Emails",
    "Public Phone Numbers",
    "Has Public Contact Info",
    "Profile Match Score",
    "Matched Skills",
    "Target Role",
    "Job URL",
    "Search Query",
    "Search Location",
    "Scraped At",
]


RELATED_SEARCH_MAP = {
    "mern": [
        "MERN developer",
        "MERN stack developer",
        "Full stack developer",
        "React Node.js developer",
        "MongoDB Express React Node developer",
    ],
    "mean": [
        "MEAN developer",
        "MEAN stack developer",
        "Angular Node.js developer",
        "Full stack developer",
    ],
    "python": [
        "Python developer",
        "Backend Python developer",
        "Django developer",
        "Flask developer",
        "FastAPI developer",
    ],
    "react": [
        "React developer",
        "Frontend developer",
        "React.js developer",
        "JavaScript developer",
    ],
    "node": [
        "Node.js developer",
        "Backend developer",
        "Express.js developer",
    ],
}


PROFILE_ROLE_MAP = {
    "mern": {
        "search_terms": [
            "MERN Stack Developer",
            "Full Stack Developer",
            "React Node.js Developer",
            "Node.js Developer",
            "Next.js Developer",
        ],
        "keywords": {
            "mern": 6,
            "full stack": 5,
            "react": 4,
            "react.js": 4,
            "node": 4,
            "node.js": 4,
            "express": 3,
            "express.js": 3,
            "mongodb": 4,
            "next.js": 3,
            "nextjs": 3,
            "javascript": 2,
            "typescript": 2,
            "redux": 2,
        },
    },
    "devops": {
        "search_terms": [
            "DevOps Engineer",
            "Cloud DevOps Engineer",
            "AWS DevOps Engineer",
            "Platform Engineer",
            "Site Reliability Engineer",
        ],
        "keywords": {
            "devops": 6,
            "docker": 4,
            "kubernetes": 5,
            "argocd": 5,
            "gitops": 4,
            "aws": 4,
            "ci/cd": 4,
            "linux": 2,
            "nginx": 2,
            "s3": 2,
        },
    },
    "ai": {
        "search_terms": [
            "AI Engineer",
            "LLM Engineer",
            "LangChain Developer",
            "Agentic AI Engineer",
            "Python AI Developer",
        ],
        "keywords": {
            "ai": 3,
            "llm": 5,
            "genai": 4,
            "langchain": 6,
            "agentic ai": 6,
            "python": 3,
            "jira automation": 4,
        },
    },
    "backend": {
        "search_terms": [
            "Backend Developer",
            "Node.js Backend Developer",
            "API Developer",
            "Software Engineer",
        ],
        "keywords": {
            "backend": 3,
            "rest": 2,
            "api": 2,
            "microservices": 3,
            "postgresql": 2,
            "redis": 2,
            "go": 1,
        },
    },
}


TITLE_PENALTIES = {
    "manager": -6,
    "lead": -4,
    "principal": -6,
    "architect": -5,
    "staff": -5,
    "director": -7,
}


EXPERIENCE_BONUSES = {
    "1 year": 1,
    "2 years": 2,
    "3 years": 3,
    "1-3 years": 3,
    "2-4 years": 3,
    "2-5 years": 3,
    "3-5 years": 2,
}


EXPERIENCE_PENALTIES = {
    "6 years": -3,
    "7 years": -4,
    "8 years": -5,
    "10 years": -6,
    "senior": -2,
}


PROFILE_COPY_IGNORE = shutil.ignore_patterns(
    "Singleton*",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "lockfile*",
    "Crash Reports",
    "GrShaderCache",
    "GraphiteDawnCache",
    "ShaderCache",
    "component_crx_cache",
    "extensions_crx_cache",
    "Safe Browsing",
    "Code Cache",
    "GPUCache",
    "Cache",
)


def build_search_url(start: int = 0) -> str:
    params = {"q": SEARCH_QUERY, "l": LOCATION}
    if start:
        params["start"] = str(start)
    return f"{INDEED_BASE}/jobs?{urlencode(params)}"


def build_search_url_for_query(search_query: str, start: int = 0) -> str:
    params = {"q": search_query, "l": LOCATION}
    if start:
        params["start"] = str(start)
    return f"{INDEED_BASE}/jobs?{urlencode(params)}"


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        return f"{INDEED_BASE}{url}"
    return url


def extract_job_key(url: str) -> str:
    if not url:
        return ""
    match = re.search(r"[?&]jk=([a-zA-Z0-9]+)", url)
    return match.group(1) if match else ""


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def split_lines(text: str) -> list[str]:
    return [clean_text(line) for line in (text or "").splitlines() if clean_text(line)]


def normalize_email(email: str) -> str:
    return clean_text(email).strip(".,;:()[]{}<>\"'").lower()


def surrounding_context(text: str, start: int, end: int, window: int = CONTACT_CONTEXT_WINDOW) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    return clean_text(text[left:right]).lower()


def has_public_contact_hint(context: str) -> bool:
    return any(hint in context for hint in PUBLIC_CONTACT_HINTS)


def is_blocked_contact_context(context: str) -> bool:
    return any(pattern in context for pattern in BLOCKED_CONTACT_CONTEXT_PATTERNS)


def is_likely_public_email(email: str, context: str) -> bool:
    local_part = email.split("@", 1)[0].lower()
    if local_part in BLOCKED_EMAIL_LOCALPARTS:
        return False
    if is_blocked_contact_context(context):
        return False
    return has_public_contact_hint(context)


def normalize_phone_number(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if not digits:
        return ""

    if raw_phone.strip().startswith("+"):
        return f"+{digits}"

    if len(digits) == 10:
        return f"{CONTACT_DEFAULT_COUNTRY_CODE}{digits}"

    if len(digits) == 11 and digits.startswith("0"):
        return f"{CONTACT_DEFAULT_COUNTRY_CODE}{digits[1:]}"

    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"

    if 8 <= len(digits) <= 15:
        return digits

    return ""


def is_likely_public_phone(raw_phone: str, normalized_phone: str, context: str) -> bool:
    if not normalized_phone:
        return False

    digit_count = len(re.sub(r"\D", "", normalized_phone))
    if digit_count < 10:
        return False
    if is_blocked_contact_context(context):
        return False
    if not has_public_contact_hint(context):
        return False

    if "fax" in context or "otp" in context or "pin" in context or "code" in context:
        return False
    if len(set(re.sub(r"\D", "", raw_phone))) == 1:
        return False

    return True


def extract_public_contacts(text: str) -> tuple[list[str], list[str]]:
    if not text:
        return [], []

    emails: list[str] = []
    seen_emails: set[str] = set()
    for match in EMAIL_REGEX.finditer(text):
        email = normalize_email(match.group(0))
        context = surrounding_context(text, match.start(), match.end())
        if not email or email in seen_emails:
            continue
        if is_likely_public_email(email, context):
            seen_emails.add(email)
            emails.append(email)

    phones: list[str] = []
    seen_phones: set[str] = set()
    for match in PHONE_CANDIDATE_REGEX.finditer(text):
        raw_phone = clean_text(match.group(1))
        normalized_phone = normalize_phone_number(raw_phone)
        context = surrounding_context(text, match.start(1), match.end(1))
        if not normalized_phone or normalized_phone in seen_phones:
            continue
        if is_likely_public_phone(raw_phone, normalized_phone, context):
            seen_phones.add(normalized_phone)
            phones.append(normalized_phone)

    return emails, phones


def normalize_key_part(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(text).lower()).strip()


def build_row_key(job_url: str, title: str, company: str, location: str) -> str:
    if job_url:
        return f"url::{normalize_url(job_url)}"
    return "meta::" + "|".join(
        [
            normalize_key_part(title),
            normalize_key_part(company),
            normalize_key_part(location),
        ]
    )


def build_search_terms(base_query: str) -> list[str]:
    if SEARCH_TERMS_OVERRIDE:
        return [clean_text(term) for term in SEARCH_TERMS_OVERRIDE if clean_text(term)]

    terms = [clean_text(base_query)]
    if AUTO_EXPAND_RELATED_SEARCHES:
        lowered = base_query.lower()
        for token, related_terms in RELATED_SEARCH_MAP.items():
            if token in lowered:
                terms.extend(related_terms)

    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        lowered = term.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique_terms.append(term)
    return unique_terms


def extract_resume_text(pdf_path: str) -> str:
    if not pdf_path or not os.path.exists(pdf_path):
        return ""
    try:
        result = subprocess.run(
            ["pdftotext", pdf_path, "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception as exc:
        log.warning("Could not extract text from resume PDF %s: %s", pdf_path, exc)
    return ""


def build_profile_from_resume(resume_text: str) -> dict:
    lowered = resume_text.lower()
    active_roles: list[str] = []
    keywords: dict[str, int] = {}
    search_terms: list[str] = []

    for role, role_data in PROFILE_ROLE_MAP.items():
        if any(keyword in lowered for keyword in role_data["keywords"]):
            active_roles.append(role)
            search_terms.extend(role_data["search_terms"])
            for keyword, weight in role_data["keywords"].items():
                keywords[keyword] = max(weight, keywords.get(keyword, 0))

    if not search_terms:
        search_terms = build_search_terms(SEARCH_QUERY)

    deduped_terms: list[str] = []
    seen_terms: set[str] = set()
    for term in [SEARCH_QUERY] + search_terms:
        cleaned = clean_text(term)
        lowered_term = cleaned.lower()
        if cleaned and lowered_term not in seen_terms:
            seen_terms.add(lowered_term)
            deduped_terms.append(cleaned)

    return {
        "roles": active_roles,
        "keywords": keywords,
        "search_terms": deduped_terms[:RESUME_SEARCH_TERMS_LIMIT],
    }


def score_job_against_profile(item: dict, profile: dict, search_term: str) -> tuple[int, str]:
    haystack = " ".join(
        [
            item.get("title", ""),
            item.get("company", ""),
            item.get("location", ""),
            item.get("salary", ""),
            item.get("job_type", ""),
            item.get("posted", ""),
            item.get("summary", ""),
            item.get("job_description", ""),
        ]
    ).lower()

    title_lower = item.get("title", "").lower()
    matched: list[str] = []
    score = 0

    for keyword, weight in profile.get("keywords", {}).items():
        if keyword in haystack:
            matched.append(keyword)
            score += weight

    if search_term and search_term.lower() in haystack:
        score += 5

    for token, penalty in TITLE_PENALTIES.items():
        if token in title_lower:
            score += penalty

    for token, bonus in EXPERIENCE_BONUSES.items():
        if token in haystack:
            score += bonus
            break

    for token, penalty in EXPERIENCE_PENALTIES.items():
        if token in haystack:
            score += penalty

    return max(score, 0), ", ".join(sorted(set(matched)))


def prepare_profile_clone(source_root: str, profile_directory: str, target_root: str) -> str:
    profile_source = os.path.join(source_root, profile_directory)
    profile_target = os.path.join(target_root, profile_directory)

    if not os.path.exists(profile_source):
        raise FileNotFoundError(f"Brave profile directory not found: {profile_source}")

    if os.path.exists(target_root):
        shutil.rmtree(target_root)
    os.makedirs(target_root, exist_ok=True)

    local_state = os.path.join(source_root, "Local State")
    if os.path.exists(local_state):
        shutil.copy2(local_state, os.path.join(target_root, "Local State"))

    first_run = os.path.join(source_root, "First Run")
    if os.path.exists(first_run):
        shutil.copy2(first_run, os.path.join(target_root, "First Run"))

    shutil.copytree(profile_source, profile_target, ignore=PROFILE_COPY_IGNORE)
    return target_root


def load_existing_keys(filename: str) -> set[str]:
    if not os.path.exists(filename):
        return set()

    keys: set[str] = set()
    try:
        with open(filename, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                keys.add(
                    build_row_key(
                        row.get("Job URL", ""),
                        row.get("Job Title", ""),
                        row.get("Company", ""),
                        row.get("Location", ""),
                    )
                )
    except Exception as exc:
        log.warning("Could not load existing CSV rows from %s: %s", filename, exc)
    return keys


def ensure_csv_schema(filename: str):
    if not os.path.exists(filename):
        return

    try:
        with open(filename, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            existing_headers = reader.fieldnames or []
            if existing_headers == CSV_HEADERS:
                return
            rows = list(reader)
    except Exception as exc:
        log.warning("Could not inspect CSV schema for %s: %s", filename, exc)
        return

    log.info("Updating CSV header schema for %s", filename)
    try:
        with open(filename, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_HEADERS)
            writer.writeheader()
            for row in rows:
                writer.writerow({header: row.get(header, "") for header in CSV_HEADERS})
    except Exception as exc:
        log.warning("Could not rewrite CSV schema for %s: %s", filename, exc)


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


async def recover_search_page(page: Page, search_url: str, reason: str) -> bool:
    if HEADLESS:
        log.warning("%s detected but headless mode cannot recover manually.", reason)
        return False

    await wait_for_manual_recovery(page, reason)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(PAGE_PAUSE_MS)

    if await is_signin_page(page) or await is_challenge_page(page):
        log.warning("Search page still blocked after manual recovery.")
        return False

    return await wait_for_search_ready(page)


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
    try:
        raw_card_text = await card.inner_text(timeout=5000)
    except Exception:
        return None
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
    job_key = extract_job_key(job_url)

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
        "public_emails": [],
        "public_phone_numbers": [],
        "has_public_contact_info": "no",
        "profile_match_score": 0,
        "matched_skills": "",
        "target_role": "",
        "job_key": job_key,
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


async def load_search_page(page: Page, url: str) -> bool:
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        log.info("Loading: %s (attempt %d/%d)", url, attempt, NAVIGATION_RETRIES)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(PAGE_PAUSE_MS)

        if await is_signin_page(page):
            log.warning("Indeed redirected to sign-in: %s", page.url)
            if not await recover_search_page(page, url, "Indeed sign-in page"):
                return False

        if await is_challenge_page(page):
            if not await recover_search_page(page, url, "Indeed verification page"):
                return False

        if await wait_for_search_ready(page):
            return True

        if attempt < NAVIGATION_RETRIES:
            wait_ms = RETRY_BACKOFF_MS * attempt
            log.info("Retrying after %dms…", wait_ms)
            await page.wait_for_timeout(wait_ms)

    log.warning("Indeed results did not appear for: %s", url)
    return False


async def find_card_for_item(page: Page, item: dict):
    job_key = item.get("job_key", "")
    if job_key:
        anchors = page.locator(f'a[data-jk="{job_key}"]')
        if await anchors.count() > 0:
            return anchors.first.locator("xpath=ancestor::*[@data-testid='slider_item'][1]")

    cards = page.locator(CARD_SELECTOR)
    count = await cards.count()
    target_title = normalize_key_part(item.get("title", ""))
    target_company = normalize_key_part(item.get("company", ""))

    for i in range(count):
        card = cards.nth(i)
        try:
            title = normalize_key_part(await safe_attr(card, 'h2 a span[title], h2 span[title]', "title"))
            if not title:
                title = normalize_key_part(await safe_text(card, "h2"))
            company = normalize_key_part(await safe_text(card, '[data-testid="company-name"]'))
            if title == target_title and company == target_company:
                return card
        except Exception:
            continue
    return None


async def enrich_with_job_detail(page: Page, item: dict, search_url: str) -> dict:
    card = await find_card_for_item(page, item)
    if card is None:
        log.warning("Could not find a matching Indeed card for %s", item.get("title", "job"))
        return item

    try:
        await card.scroll_into_view_if_needed(timeout=5000)
        await page.wait_for_timeout(400)
        await card.click()
        if await is_signin_page(page) or await is_challenge_page(page):
            if not await recover_search_page(page, search_url, "Indeed verification after opening job"):
                return item
            card = await find_card_for_item(page, item)
            if card is None:
                return item
            await card.scroll_into_view_if_needed(timeout=5000)
            await page.wait_for_timeout(400)
            await card.click()
        if not await wait_for_job_detail(page):
            return item
    except Exception as exc:
        log.warning("Could not open Indeed detail for %s: %s", item.get("title", "job"), exc)
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
        public_emails, public_phones = extract_public_contacts(detail_description)
        item["public_emails"] = public_emails
        item["public_phone_numbers"] = public_phones
        item["has_public_contact_info"] = "yes" if public_emails or public_phones else "no"

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


async def scrape_search_page(page: Page, url: str, profile: dict, search_term: str) -> list[dict]:
    if not await load_search_page(page, url):
        return []

    cards = page.locator(CARD_SELECTOR)
    basic_items: list[dict] = []
    results: list[dict] = []
    count = await cards.count()
    log.info("Visible Indeed cards: %d", count)
    for i in range(count):
        item = await extract_card(cards.nth(i))
        if item:
            basic_items.append(item)

    for batch_start in range(0, len(basic_items), DETAIL_BATCH_SIZE):
        batch = basic_items[batch_start:batch_start + DETAIL_BATCH_SIZE]
        if batch_start > 0:
            log.info("Reloading search results for next detail batch at item %d", batch_start + 1)
            if not await load_search_page(page, url):
                break

        for item in batch:
            item = await enrich_with_job_detail(page, item, url)
            item["target_role"] = search_term
            score, matched = score_job_against_profile(item, profile, search_term)
            item["profile_match_score"] = score
            item["matched_skills"] = matched
            if score >= MIN_PROFILE_MATCH_SCORE:
                results.append(item)

    results.sort(key=lambda row: row["profile_match_score"], reverse=True)
    return results


def save_rows(rows: list[list], filename: str = OUTPUT_CSV):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(CSV_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to %s", len(rows), filename)


def flush_pending_rows(pending_rows: list[list], filename: str = OUTPUT_CSV):
    if not pending_rows:
        return
    save_rows(pending_rows, filename=filename)
    pending_rows.clear()


async def run():
    if not os.path.exists(BRAVE_USER_DATA_DIR):
        raise FileNotFoundError(f"Brave user data dir not found: {BRAVE_USER_DATA_DIR}")
    if not os.path.exists(BRAVE_EXECUTABLE):
        raise FileNotFoundError(f"Brave executable not found: {BRAVE_EXECUTABLE}")

    log.info("=" * 60)
    log.info("Indeed Brave Profile Scraper")
    resume_text = extract_resume_text(RESUME_PDF) if USE_RESUME_MATCHING else ""
    profile = build_profile_from_resume(resume_text) if resume_text else {"roles": [], "keywords": {}, "search_terms": []}
    search_terms = profile["search_terms"] if profile.get("search_terms") else build_search_terms(SEARCH_QUERY)
    ensure_csv_schema(OUTPUT_CSV)
    existing_keys = load_existing_keys(OUTPUT_CSV)
    log.info("Query   : %s", SEARCH_QUERY)
    log.info("Location: %s", LOCATION)
    log.info("Max     : %d results", MAX_RESULTS)
    log.info("Brave   : %s", BRAVE_EXECUTABLE)
    log.info("Profile : %s (%s)", BRAVE_PROFILE_DIRECTORY, BRAVE_USER_DATA_DIR)
    log.info("Terms   : %s", " | ".join(search_terms))
    log.info("Resume  : %s", RESUME_PDF if resume_text else "not used")
    log.info("Roles   : %s", ", ".join(profile.get("roles", [])) or "none detected")
    log.info("Existing rows detected in CSV: %d", len(existing_keys))
    log.info("=" * 60)
    log.info("Close Brave fully before running this script to avoid profile lock issues.")

    launch_user_data_dir = BRAVE_USER_DATA_DIR
    if USE_PROFILE_CLONE:
        launch_user_data_dir = prepare_profile_clone(
            BRAVE_USER_DATA_DIR,
            BRAVE_PROFILE_DIRECTORY,
            PROFILE_CLONE_ROOT,
        )
        log.info("Using temporary cloned Brave profile: %s", launch_user_data_dir)

    rows: list[list] = []
    pending_rows: list[list] = []
    seen_keys: set[str] = set(existing_keys)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=launch_user_data_dir,
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

        for search_term in search_terms:
            log.info("Starting search term: %s", search_term)
            for start in range(0, MAX_RESULTS, RESULTS_PER_PAGE):
                page_rows = await scrape_search_page(
                    page,
                    build_search_url_for_query(search_term, start=start),
                    profile=profile,
                    search_term=search_term,
                )
                if not page_rows:
                    break

                new_rows = 0
                for item in page_rows:
                    job_url = item["job_url"]
                    key = build_row_key(job_url, item["title"], item["company"], item["location"])
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    rows.append([
                        item["title"],
                        item["company"],
                        item["location"],
                        item["salary"],
                        item["job_type"],
                        item["posted"],
                        item["summary"],
                        item["job_description"],
                        ", ".join(item["public_emails"]),
                        ", ".join(item["public_phone_numbers"]),
                        item["has_public_contact_info"],
                        item["profile_match_score"],
                        item["matched_skills"],
                        item["target_role"],
                        job_url,
                        search_term,
                        LOCATION,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ])
                    pending_rows.append(rows[-1])
                    new_rows += 1

                    log.info(
                        "[%03d] %s | %s | %s | term=%s",
                        len(rows),
                        item["title"] or "-",
                        item["company"] or "-",
                        item["location"] or "-",
                        search_term,
                    )
                    if CONTINUOUS_SAVE and len(pending_rows) >= SAVE_EVERY_N_ROWS:
                        flush_pending_rows(pending_rows, filename=OUTPUT_CSV)
                    if len(rows) >= MAX_RESULTS:
                        break

                if new_rows == 0 or len(rows) >= MAX_RESULTS:
                    break

            if len(rows) >= MAX_RESULTS:
                break

        await context.close()

    if rows:
        if pending_rows:
            flush_pending_rows(pending_rows, filename=OUTPUT_CSV)
        if not CONTINUOUS_SAVE:
            rows.sort(key=lambda row: row[11], reverse=True)
            save_rows(rows[:MAX_RESULTS])
    else:
        log.warning("No Indeed results collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run())
