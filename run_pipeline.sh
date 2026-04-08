#!/bin/bash
# Runs twice a week via launchd (or any scheduler):
# 1. Scrape new files from Stud.IP
# 2. Summarise new files with Claude

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$(command -v python3)}"
LOG="$SCRIPT_DIR/pipeline.log"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') Pipeline started" >> "$LOG"

echo "--- Scraper ---" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/scraper.py" >> "$LOG" 2>&1

echo "--- Summarizer ---" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/summarize.py" >> "$LOG" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') Pipeline done" >> "$LOG"
