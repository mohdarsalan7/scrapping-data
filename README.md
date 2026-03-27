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

## Requirements

Use the existing virtual environment or create one:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_free.txt
playwright install chromium
```

## Common Usage

Edit the configuration section at the top of the script before running.

Common values to change:

- `SEARCH_QUERY`
- `LOCATION`
- `MAX_RESULTS`
- `HEADLESS`

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

Notes:

- This mode is best when your Indeed login already exists in Brave.
- If Indeed shows sign-in or verification in the opened Brave window, complete it there and let the script continue.
- This is the best script in this folder for collecting real Indeed job descriptions.

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
