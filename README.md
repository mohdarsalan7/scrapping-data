# Scraping Tools

This folder contains separate standalone scraping scripts.

## Files

- `gmaps_playwright.py`
  Google Maps business scraper.
- `indeed_playwright.py`
  Indeed search-results scraper without browser profile login.
- `indeed_brave_profile.py`
  Indeed scraper that opens Brave in headful mode and uses your signed-in Brave profile.
- `naukri_playwright.py`
  Naukri scraper with access-denied detection.
- `osm_scraper.py`
  OSM-based alternative scraper.
- `website_crawler_scraper.py`
  Internal-route website crawler for public emails, phone numbers, page metadata, and keywords.

## Requirements

Use the existing virtual environment or create one:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_free.txt
playwright install chromium
```

Optional Gemini-powered query generation:

1. Copy `.env.example` to `.env`
2. Set `GEMINI_API_KEY`
3. Run the Indeed Brave script normally

When `GEMINI_API_KEY` or `GOOGLE_API_KEY` is present, `indeed_brave_profile.py` uses the CV text to generate better Indeed query options and then falls back to the built-in heuristic terms if Gemini is unavailable.

## Common Usage

Edit the configuration section at the top of the script before running.

Common values to change:

- `SEARCH_QUERY`
- `SEARCH_QUERIES`
- `TARGET_URL`
- `LOCATION`
- `MAX_RESULTS`
- `HEADLESS`
- `RESUME_PDF`
- `USE_GEMINI_QUERY_GENERATION`

Run any script like this:

```bash
./.venv/bin/python script_name.py
```

## 1. Google Maps

File:

- `gmaps_playwright.py`

Run:

```bash
./.venv/bin/python gmaps_playwright.py
```

Output:

- `gmaps_leads.csv`
- `scraper.log`

Notes:

- Falls back to CSV if `service_account.json` is missing or empty.
- If you want Google Sheets export, add a valid service account JSON file.
- The scraper keeps the Google Maps phone when available, normalizes it, and also checks the business website plus common contact pages for public emails and extra phone numbers.
- It now dedupes using the Maps URL first, then business metadata, and saves new leads continuously during long runs.
- Website contact extraction is intentionally filtered so raw HTML asset names, tracking IDs, and long numeric blobs are less likely to be saved as emails or phone numbers.
- You can now set multiple `SEARCH_QUERIES` in one run to combine several Google Maps searches into one deduped output file.

## 2. Indeed Without Profile

File:

- `indeed_playwright.py`

Run:

```bash
./.venv/bin/python indeed_playwright.py
```

Output:

- `indeed_jobs.csv`
- `indeed_scraper.log`

Notes:

- Works only with the visible anonymous/search-result cards.
- Indeed may redirect later pages to sign-in.
- Full job descriptions are not reliable in this mode.

## Website Crawler

File:

- `website_crawler_scraper.py`

Run:

```bash
./.venv/bin/python website_crawler_scraper.py
```

Output:

- `website_crawl_pages.csv`
- `website_crawl_summary.json`
- `website_crawler_scraper.log`

Notes:

- Set `TARGET_URL` at the top of the script, for example `https://zobsai.com`.
- The crawler stays on internal routes for the same site and can optionally include subdomains.
- It extracts public emails and phone numbers, page titles, meta descriptions, H1 text, and top keywords from visible page text.
- `MAX_PAGES` controls the crawl depth cap for one run.

## 3. Indeed With Signed-In Brave Profile

File:

- `indeed_brave_profile.py`

Run:

```bash
./.venv/bin/python indeed_brave_profile.py
```

Output:

- `indeed_brave_profile_jobs.csv`
- `indeed_brave_profile.log`

Before running:

1. Close Brave fully.
2. Make sure the script points to the correct Brave profile.

Important config:

- `BRAVE_EXECUTABLE`
- `BRAVE_USER_DATA_DIR`
- `BRAVE_PROFILE_DIRECTORY`
- `HEADLESS = False`
- `RESUME_PDF`
- `GEMINI_API_KEY` in `.env` or shell env

Notes:

- This mode is best when your Indeed login already exists in Brave.
- If Indeed shows sign-in or verification in the opened Brave window, complete it there and let the script continue.
- This is the best script in this folder for collecting real Indeed job descriptions.
- The CSV now also includes `Public Emails`, `Public Phone Numbers`, and `Has Public Contact Info` extracted only from visible job-description text.
- Contact extraction is intentionally conservative: it keeps recruiter-style contact info with nearby public contact hints like `email`, `call`, `whatsapp`, or `share resume`, and skips obvious no-reply or blocked-contact text.
- With Gemini enabled, the script reads the resume, proposes role-specific Indeed queries, and still falls back to the local rules if the API call fails.

## 4. Naukri

File:

- `naukri_playwright.py`

Run:

```bash
./.venv/bin/python naukri_playwright.py
```

Output:

- `naukri_jobs.csv`
- `naukri_scraper.log`

Notes:

- Naukri often returns `Access Denied` for automated traffic.
- The script detects that and exits cleanly.
- If blocked, try headful mode, a different network, or a warmer browser profile.

## 5. OSM Alternative

File:

- `osm_scraper.py`

Run:

```bash
./.venv/bin/python osm_scraper.py
```

Notes:

- Use this if you want a simpler non-Google business scraping path.

## Clean Run

Most scripts append to existing CSV files.

If you want a fresh output, remove the old CSV first:

```bash
rm -f gmaps_leads.csv indeed_jobs.csv indeed_brave_profile_jobs.csv naukri_jobs.csv
```

## Quick Start

Google Maps:

```bash
./.venv/bin/python gmaps_playwright.py
```

Indeed with Brave profile:

```bash
./.venv/bin/python indeed_brave_profile.py
```

Naukri:

```bash
./.venv/bin/python naukri_playwright.py
```
