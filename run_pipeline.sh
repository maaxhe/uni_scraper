#!/bin/bash
# Läuft zweimal pro Woche via launchd:
# 1. Neue Dateien von Stud.IP scrapen
# 2. Neue Dateien mit Claude zusammenfassen

SCRIPT_DIR="/Users/maxmacbookpro/Developer/eigene Projekte/uni_scraper"
PYTHON="/opt/homebrew/bin/python3"
LOG="$SCRIPT_DIR/pipeline.log"

echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') Pipeline gestartet" >> "$LOG"

echo "--- Scraper ---" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/scraper.py" >> "$LOG" 2>&1

echo "--- Summarizer ---" >> "$LOG"
"$PYTHON" "$SCRIPT_DIR/summarize.py" >> "$LOG" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') Pipeline beendet" >> "$LOG"
