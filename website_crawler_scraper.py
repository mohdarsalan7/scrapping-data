"""
Website Crawler Scraper
=======================
Crawls a website's internal routes and extracts public contact info,
page metadata, and keyword summaries.

Good for:
- finding public emails and phone numbers across a website
- discovering useful internal routes
- summarizing headings, descriptions, and high-frequency keywords

Run:
    python website_crawler_scraper.py
"""

import csv
import html
import json
import logging
import os
import re
from collections import Counter, deque
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests


# Configuration
TARGET_URL = "https://zobsai.com"
MAX_PAGES = 100
REQUEST_TIMEOUT_SEC = 12
MAX_KEYWORDS = 25
INCLUDE_SUBDOMAINS = False

OUTPUT_SUMMARY_JSON = "website_crawl_summary.json"
OUTPUT_PAGES_CSV = "website_crawl_pages.csv"
LOG_FILE = "website_crawler_scraper.log"

HEADERS_BOT = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"(?:(?<=\D)|^)(\+?\d[\d\s().-]{7,}\d)(?=\D|$)")
MAILTO_REGEX = re.compile(r'(?i)mailto:([^"\'<>\s?#]+)')
TEL_REGEX = re.compile(r'(?i)tel:([^"\'<>\s?#]+)')

BLOCKED_EMAIL_LOCALPARTS = {
    "abuse", "mailer-daemon", "news", "newsletter", "no-reply", "nobody",
    "noreply", "notifications", "postmaster", "unsubscribe", "updates",
}
BLOCKED_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "ico",
    "css", "js", "json", "xml", "map", "txt", "woff", "woff2", "ttf", "otf",
}
NOISE_DOMAINS = {
    "example.com", "schema.org", "sentry.io", "w3.org", "cloudflare.com",
    "google.com", "googletagmanager.com", "gstatic.com",
}
CONTACT_HINTS = [
    "call", "contact", "customer care", "email", "mobile", "phone",
    "reach us", "sales", "support", "tel", "whatsapp",
]
COMMON_INDIA_STD_CODES_2 = {"11", "20", "22", "33", "40", "44", "79", "80"}
COMMON_INDIA_STD_CODES_3 = {"120", "124", "129", "135", "141", "172", "175", "183"}

PAGE_HEADERS = [
    "URL",
    "Status Code",
    "Page Title",
    "Meta Description",
    "Meta Keywords",
    "H1",
    "Emails Found",
    "Phone Numbers Found",
    "Top Keywords",
    "Word Count",
    "Internal Links Found",
    "Scraped At",
]

STOPWORDS = {
    "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "be",
    "been", "being", "by", "can", "for", "from", "get", "has", "have", "how",
    "if", "in", "into", "is", "it", "its", "more", "new", "not", "of", "on",
    "or", "our", "out", "that", "the", "their", "them", "there", "these",
    "they", "this", "to", "up", "us", "was", "we", "what", "when", "where",
    "which", "who", "why", "will", "with", "you", "your",
}


class HTMLContentExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_ignored_tag = False
        self.ignored_tag_stack: list[str] = []
        self.current_title = False
        self.current_heading: str | None = None
        self.title_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.headings: dict[str, list[str]] = {f"h{i}": [] for i in range(1, 7)}
        self.links: list[str] = []
        self.meta_description = ""
        self.meta_keywords = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        lower_tag = tag.lower()

        if lower_tag in {"script", "style", "noscript", "svg", "canvas", "iframe"}:
            self.in_ignored_tag = True
            self.ignored_tag_stack.append(lower_tag)
            return

        if lower_tag == "title":
            self.current_title = True
            return

        if lower_tag in self.headings:
            self.current_heading = lower_tag
            return

        if lower_tag == "meta":
            meta_name = (attrs_dict.get("name") or attrs_dict.get("property") or "").strip().lower()
            meta_content = (attrs_dict.get("content") or "").strip()
            if meta_name == "description" and meta_content and not self.meta_description:
                self.meta_description = meta_content
            if meta_name == "keywords" and meta_content and not self.meta_keywords:
                self.meta_keywords = meta_content
            return

        if lower_tag == "a":
            href = (attrs_dict.get("href") or "").strip()
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        lower_tag = tag.lower()
        if lower_tag == "title":
            self.current_title = False
        if self.current_heading == lower_tag:
            self.current_heading = None
        if self.ignored_tag_stack and lower_tag == self.ignored_tag_stack[-1]:
            self.ignored_tag_stack.pop()
            self.in_ignored_tag = bool(self.ignored_tag_stack)

    def handle_data(self, data):
        if self.in_ignored_tag:
            return

        text = clean_text(data)
        if not text:
            return

        if self.current_title:
            self.title_parts.append(text)
        elif self.current_heading:
            self.headings[self.current_heading].append(text)
            self.visible_parts.append(text)
        else:
            self.visible_parts.append(text)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def normalize_email(email: str) -> str:
    return clean_text(email).strip(".,;:()[]{}<>\"'").lower()


def get_root_domain(netloc: str) -> str:
    parts = netloc.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc.lower()


def is_internal_url(url: str, start_url: str) -> bool:
    parsed_url = urlparse(url)
    parsed_start = urlparse(start_url)

    if parsed_url.scheme not in {"http", "https"}:
        return False
    if not parsed_url.netloc:
        return True
    if parsed_url.netloc.lower() == parsed_start.netloc.lower():
        return True
    if INCLUDE_SUBDOMAINS and get_root_domain(parsed_url.netloc) == get_root_domain(parsed_start.netloc):
        return True
    return False


def normalize_internal_link(href: str, base_url: str, start_url: str) -> str:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    joined = urljoin(base_url, href)
    parsed = urlparse(joined)
    cleaned = parsed._replace(fragment="", query="")
    normalized = normalize_url(cleaned.geturl())
    if not normalized:
        return ""
    if not is_internal_url(normalized, start_url):
        return ""
    return normalized


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
    if not digits or not looks_like_india_phone_digits(digits):
        return ""
    if raw_phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1800"):
        return digits
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
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


def extract_keywords(text: str, limit: int = MAX_KEYWORDS) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text.lower())
    filtered = [
        word for word in words
        if word not in STOPWORDS
        and not word.isdigit()
        and len(word) >= 3
    ]
    counts = Counter(filtered)
    return [word for word, _ in counts.most_common(limit)]


def parse_page_content(page_url: str, raw_html: str, start_url: str) -> dict:
    extractor = HTMLContentExtractor()
    extractor.feed(raw_html)

    title = clean_text(" ".join(extractor.title_parts))
    visible_text = "\n".join(extractor.visible_parts)
    h1_list = [clean_text(item) for item in extractor.headings["h1"] if clean_text(item)]

    emails: set[str] = set()
    phones: set[str] = set()

    for href in extractor.links:
        lowered = href.lower()
        if lowered.startswith("mailto:"):
            normalized_email = normalize_email(unquote(href[7:]).split("?", 1)[0])
            if is_valid_email(normalized_email):
                emails.add(normalized_email)
            continue

        if lowered.startswith("tel:"):
            raw_phone = unquote(href[4:]).split("?", 1)[0]
            if not is_valid_phone(raw_phone):
                continue
            normalized_phone = normalize_phone_number(raw_phone)
            if normalized_phone:
                phones.add(normalized_phone)

    visible_emails, visible_phones = extract_contacts_from_visible_text(visible_text)
    emails.update(visible_emails)
    phones.update(visible_phones)

    internal_links: list[str] = []
    seen_links: set[str] = set()
    for href in extractor.links:
        normalized_link = normalize_internal_link(href, base_url=page_url, start_url=start_url)
        if not normalized_link or normalized_link in seen_links:
            continue
        seen_links.add(normalized_link)
        internal_links.append(normalized_link)

    keywords = extract_keywords(
        " ".join(
            part for part in [
                title,
                extractor.meta_description,
                extractor.meta_keywords,
                visible_text,
            ] if part
        )
    )

    return {
        "url": page_url,
        "page_title": title,
        "meta_description": clean_text(extractor.meta_description),
        "meta_keywords": clean_text(extractor.meta_keywords),
        "h1": h1_list,
        "emails_found": sorted(emails),
        "phone_numbers_found": sorted(phones),
        "top_keywords": keywords,
        "word_count": len(re.findall(r"\b\w+\b", visible_text)),
        "internal_links": internal_links,
    }


def save_pages_csv(rows: list[list], filename: str = OUTPUT_PAGES_CSV):
    write_header = not os.path.exists(filename)
    with open(filename, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if write_header:
            writer.writerow(PAGE_HEADERS)
        writer.writerows(rows)
    log.info("Saved %d rows to %s", len(rows), filename)


def save_summary_json(summary: dict, filename: str = OUTPUT_SUMMARY_JSON):
    with open(filename, "w", encoding="utf-8") as jsonfile:
        json.dump(summary, jsonfile, ensure_ascii=True, indent=2)
    log.info("Saved crawl summary to %s", filename)


def crawl_site(start_url: str) -> tuple[dict, list[list]]:
    normalized_start_url = normalize_url(start_url)
    queue: deque[str] = deque([normalized_start_url])
    visited_urls: set[str] = set()
    site_emails: set[str] = set()
    site_phones: set[str] = set()
    site_keywords: Counter[str] = Counter()
    pages_data: list[dict] = []
    page_rows: list[list] = []
    session = requests.Session()

    while queue and len(visited_urls) < MAX_PAGES:
        current_url = queue.popleft()
        if current_url in visited_urls:
            continue
        visited_urls.add(current_url)

        try:
            response = session.get(current_url, headers=HEADERS_BOT, timeout=REQUEST_TIMEOUT_SEC)
            status_code = response.status_code
            content_type = (response.headers.get("content-type") or "").lower()
            if status_code != 200 or "html" not in content_type:
                log.info("Skipping non-HTML or non-200 page: %s (%s)", current_url, status_code)
                continue

            page_data = parse_page_content(current_url, response.text, normalized_start_url)
            page_data["status_code"] = status_code
            pages_data.append(page_data)

            for email in page_data["emails_found"]:
                site_emails.add(email)
            for phone in page_data["phone_numbers_found"]:
                site_phones.add(phone)
            site_keywords.update(page_data["top_keywords"])

            for link in page_data["internal_links"]:
                if link not in visited_urls:
                    queue.append(link)

            page_rows.append([
                page_data["url"],
                page_data["status_code"],
                page_data["page_title"],
                page_data["meta_description"],
                page_data["meta_keywords"],
                " | ".join(page_data["h1"]),
                ", ".join(page_data["emails_found"]),
                ", ".join(page_data["phone_numbers_found"]),
                ", ".join(page_data["top_keywords"][:10]),
                page_data["word_count"],
                len(page_data["internal_links"]),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ])

            log.info(
                "[%03d/%03d] %s | emails=%d | phones=%d | links=%d",
                len(pages_data),
                MAX_PAGES,
                current_url,
                len(page_data["emails_found"]),
                len(page_data["phone_numbers_found"]),
                len(page_data["internal_links"]),
            )
        except Exception as exc:
            log.warning("Failed to crawl %s: %s", current_url, exc)

    summary = {
        "target_url": normalized_start_url,
        "pages_crawled": len(pages_data),
        "max_pages": MAX_PAGES,
        "public_emails": sorted(site_emails),
        "public_phone_numbers": sorted(site_phones),
        "top_keywords": [keyword for keyword, _ in site_keywords.most_common(MAX_KEYWORDS)],
        "pages": pages_data,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return summary, page_rows


def run():
    log.info("=" * 60)
    log.info("Website Crawler Scraper")
    log.info("Target   : %s", TARGET_URL)
    log.info("Max Pages: %d", MAX_PAGES)
    log.info("=" * 60)

    summary, page_rows = crawl_site(TARGET_URL)

    if page_rows:
        save_pages_csv(page_rows, filename=OUTPUT_PAGES_CSV)
        save_summary_json(summary, filename=OUTPUT_SUMMARY_JSON)
    else:
        log.warning("No crawlable HTML pages were collected.")

    log.info("=" * 60)
    log.info("Done.")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
