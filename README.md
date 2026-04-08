# Stud.IP Dashboard

Automatically downloads all files from your Stud.IP courses, summarises them with Claude AI, and serves a local learning dashboard.

## What's included

| Script | What it does |
|---|---|
| `scraper.py` | Downloads all course files from Stud.IP via the REST API |
| `summarize.py` | Sends course files to Claude and writes a Markdown summary |
| `dashboard.py` | Local web app — file browser, summaries, flashcards, notes, AI chat |
| `run_pipeline.sh` | Runs scraper + summarizer in sequence (for cron / launchd) |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
STUDIP_USERNAME=your_uni_username
STUDIP_PASSWORD=your_password          # wrap special chars in quotes: "pass#word"
ANTHROPIC_API_KEY=sk-ant-...           # https://console.anthropic.com
COURSES_DIR=/path/to/your/courses      # where files are saved and read from
```

---

## Usage

### Dashboard (recommended starting point)

```bash
python dashboard.py
```

Open **http://localhost:5001** — scraping and summarising can be triggered directly from the UI.

---

### Scraper — download course files

```bash
# All semesters
python scraper.py

# Single course by URL
python scraper.py --url "https://studip.uni-osnabrueck.de/dispatch.php/course/overview?cid=..."

# Watch the browser (useful for debugging login)
python scraper.py --no-headless
```

Files are saved under `$COURSES_DIR/<semester>/<course>/`.

---

### Summarizer — generate AI summaries

```bash
# All courses (skips already summarised ones)
python summarize.py

# Specific course
python summarize.py --course "machine learning"

# Force regenerate
python summarize.py --course "machine learning" --force

# Create additional summary alongside existing one
python summarize.py --course "machine learning" --out _zusammenfassung_v2.md --force

# German output
python summarize.py --lang de

# Limit files processed per course (default: 3)
python summarize.py --limit 10
```

Summaries are saved as `_zusammenfassung.md` inside each course folder. Multiple summaries per course are supported — the dashboard always shows the most recent one by default.

---

### Automated pipeline (cron / launchd)

```bash
chmod +x run_pipeline.sh
# Add to crontab, e.g. twice a week:
# 0 8 * * 1,4 /path/to/uni_scraper/run_pipeline.sh
```

The script auto-detects Python, loads `.env`, and appends output to `pipeline.log`.

---

## Notes

- Already-downloaded files are never re-downloaded.
- Subfolders within a course are mirrored locally.
- Credentials stay in `.env` and are never transmitted anywhere except to your university's SSO.
- The dashboard runs entirely locally — no data leaves your machine except API calls to Anthropic.
