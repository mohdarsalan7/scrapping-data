"""
OpenStreetMap + Overpass API Scraper — 100% Free & Legal
=========================================================
Uses the public Overpass API (OpenStreetMap data) to find businesses.
No API key. No cost. No ToS issues.

Limitations vs Google Maps:
- Phone numbers: only if the business added them to OSM (partial)
- Coverage: excellent in cities, thinner in rural areas
- No star ratings

Good for: building a base list of businesses, then enriching
          with emails by visiting their websites.

Install:
    pip install requests gspread google-auth

Run:
    python osm_scraper.py
"""

import re
import time
import logging
import requests
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

# City to search in (used for Nominatim geocoding)
SEARCH_CITY         = "New Delhi, India"

# OSM business type — see https://wiki.openstreetmap.org/wiki/Key:shop
# or https://wiki.openstreetmap.org/wiki/Key:office
OSM_KEY_VALUE_PAIRS = [
    ("office", "it"),              # IT companies
    ("office", "web_development"), # Web dev agencies
    ("office", "advertising"),     # Ad / creative agencies
    ("office", "company"),         # Generic companies
]

# Radius around city center in meters
SEARCH_RADIUS_M     = 15_000

# Google Sheets
SERVICE_ACCOUNT_FILE = "service_account.json"
SPREADSHEET_NAME     = "OSM Business Leads"

# Request delays
OVERPASS_PAUSE_SEC  = 2.0
WEBSITE_TIMEOUT_SEC = 8

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("osm_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# EMAIL EXTRACTION
# ─────────────────────────────────────────────

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
NOISE_DOMAINS = {
    "example.com","sentry.io","w3.org","schema.org","cloudflare.com",
    "google.com","wixpress.com","wordpress.org","jquery.com",
}
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us"]
HEADERS_BOT = {"User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0; +mailto:you@youragency.com)"}

def extract_emails(url: str) -> set[str]:
    if not url:
        return set()
    if not url.startswith("http"):
        url = "https://" + url
    found: set[str] = set()
    base = url.rstrip("/")
    for path in [""] + CONTACT_PATHS:
        try:
            r = requests.get(base + path, headers=HEADERS_BOT, timeout=WEBSITE_TIMEOUT_SEC)
            if r.status_code == 200:
                for e in EMAIL_REGEX.findall(r.text):
                    domain = e.split("@")[-1].lower()
                    if domain not in NOISE_DOMAINS and "." in domain:
                        found.add(e.lower())
        except Exception:
            pass
    return found

# ─────────────────────────────────────────────
# OVERPASS / OSM HELPERS
# ─────────────────────────────────────────────

def geocode_city(city: str) -> tuple[float, float]:
    """Return (lat, lon) for a city name via Nominatim."""
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": city, "format": "json", "limit": 1},
        headers={"User-Agent": "LeadBot/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Could not geocode: {city}")
    return float(data[0]["lat"]), float(data[0]["lon"])


def build_overpass_query(lat: float, lon: float, radius: int, key: str, value: str) -> str:
    """Build an Overpass QL query for a key=value tag within a radius."""
    return f"""
[out:json][timeout:30];
(
  node["{key}"="{value}"](around:{radius},{lat},{lon});
  way["{key}"="{value}"](around:{radius},{lat},{lon});
  relation["{key}"="{value}"](around:{radius},{lat},{lon});
);
out center tags;
"""


def fetch_overpass(query: str) -> list[dict]:
    """Run an Overpass query and return the list of elements."""
    resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=40)
    resp.raise_for_status()
    return resp.json().get("elements", [])


def parse_element(el: dict) -> dict:
    """Extract useful fields from an OSM element."""
    tags = el.get("tags", {})
    return {
        "name":    tags.get("name", ""),
        "phone":   tags.get("phone") or tags.get("contact:phone", ""),
        "website": tags.get("website") or tags.get("contact:website", ""),
        "email":   tags.get("email") or tags.get("contact:email", ""),
        "address": ", ".join(filter(None, [
            tags.get("addr:housenumber", ""),
            tags.get("addr:street", ""),
            tags.get("addr:city", ""),
            tags.get("addr:state", ""),
            tags.get("addr:country", ""),
        ])),
        "osm_type": el.get("type", ""),
        "osm_id":   str(el.get("id", "")),
    }

# ─────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────

SHEET_HEADERS = [
    "Business Name", "Address", "Phone",
    "Website", "OSM Email", "Scraped Emails",
    "OSM Type", "OSM ID", "Scraped At",
]
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def init_sheet():
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

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("OpenStreetMap Free Lead Scraper")
    log.info(f"City  : {SEARCH_CITY}  |  Radius: {SEARCH_RADIUS_M} m")
    log.info("=" * 60)

    sheet = init_sheet()
    seen_ids: set[str] = set()
    rows: list[list] = []

    # Geocode city
    lat, lon = geocode_city(SEARCH_CITY)
    log.info(f"Geocoded '{SEARCH_CITY}' → ({lat:.4f}, {lon:.4f})")

    # Query for each OSM tag
    for key, value in OSM_KEY_VALUE_PAIRS:
        log.info(f"Querying: {key}={value} ...")
        query = build_overpass_query(lat, lon, SEARCH_RADIUS_M, key, value)

        try:
            elements = fetch_overpass(query)
        except Exception as e:
            log.error(f"Overpass query failed: {e}")
            elements = []

        log.info(f"  Got {len(elements)} results")

        for el in elements:
            parsed = parse_element(el)
            uid = f"{parsed['osm_type']}/{parsed['osm_id']}"

            if uid in seen_ids or not parsed["name"]:
                continue
            seen_ids.add(uid)

            # Supplement emails from OSM tag OR website
            osm_email = parsed["email"]
            scraped_emails = extract_emails(parsed["website"])
            if osm_email:
                scraped_emails.discard(osm_email)
            emails_str = ", ".join(sorted(scraped_emails))

            row = [
                parsed["name"],
                parsed["address"],
                parsed["phone"],
                parsed["website"],
                osm_email,
                emails_str,
                parsed["osm_type"],
                parsed["osm_id"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ]
            rows.append(row)

            log.info(
                f"  [{len(rows):03d}] {parsed['name'][:38]:<38}  "
                f"✉ {osm_email or emails_str or '—'}"
            )

        time.sleep(OVERPASS_PAUSE_SEC)

    if rows:
        sheet.append_rows(rows, value_input_option="RAW")
        log.info(f"\n✅ Exported {len(rows)} leads to '{SPREADSHEET_NAME}'")
    else:
        log.warning("No results found. Try broadening the OSM tags.")

    log.info("=" * 60)

if __name__ == "__main__":
    run()
