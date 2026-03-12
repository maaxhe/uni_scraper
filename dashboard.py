"""
Stud.IP Dashboard — lokales Lern-Panel.
Starten: python dashboard.py  →  http://localhost:5001
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import markdown2
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request, send_file

load_dotenv()

COURSES_DIR      = Path("/Users/maxmacbookpro/Documents/Uni/Cognitive Science [Course]/Courses")
PYTHON           = sys.executable
SUMMARIZE_SCRIPT = str(Path(__file__).parent / "summarize.py")
SCRAPER_SCRIPT   = str(Path(__file__).parent / "scraper.py")
PIPELINE_LOG     = str(Path(__file__).parent / "pipeline.log")
OUTPUT_FILENAME  = "_zusammenfassung.md"
NOTES_FILENAME   = "_notizen.md"
PROGRESS_FILE    = Path(__file__).parent / "progress.json"
SUPPORTED_EXT    = {".pdf", ".docx", ".txt", ".md", ".pptx"}

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Progress store
# ---------------------------------------------------------------------------

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}

def save_progress(data: dict):
    PROGRESS_FILE.write_text(json.dumps(data, indent=2))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_courses():
    result = []
    progress = load_progress()
    for d in sorted(COURSES_DIR.iterdir()):
        if not d.is_dir():
            continue
        files = list_files(d)
        summary_path = d / OUTPUT_FILENAME
        notes_path   = d / NOTES_FILENAME
        p = progress.get(d.name, {})
        total_q  = p.get("total", 0)
        known_q  = p.get("known", 0)
        result.append({
            "name":        d.name,
            "has_summary": summary_path.exists(),
            "has_notes":   notes_path.exists(),
            "file_count":  len(files),
            "summary_age": int(summary_path.stat().st_mtime) if summary_path.exists() else None,
            "progress":    {"total": total_q, "known": known_q},
        })
    return result

def list_files(course_dir: Path) -> list[str]:
    return sorted([
        f.name for f in course_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXT
        and f.name != OUTPUT_FILENAME
        and f.name != NOTES_FILENAME
        and ".summary" not in f.name
    ])

def read_file_text(course_name: str, filename: str) -> str:
    path = COURSES_DIR / course_name / filename
    if not path.exists():
        return ""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            import fitz
            doc = fitz.open(str(path))
            return "\n\n".join(page.get_text() for page in doc)
        elif suffix == ".docx":
            from docx import Document
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        elif suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Fehler beim Lesen: {e}"
    return ""

def parse_flashcards(summary_md: str) -> list[dict]:
    """Extract Q&A pairs from summary markdown."""
    cards = []
    # Find all sections (split by "## [Dateiname]")
    sections = re.split(r'\n## ', summary_md)
    for section in sections:
        lines = section.strip().split('\n')
        section_title = lines[0].strip('# ').strip() if lines else "Unbekannt"

        # Extract questions
        q_block = re.search(r'\*\*Trainingsfragen\*\*\s*\n(.*?)(?=\n\*\*|\Z)', section, re.DOTALL)
        a_block = re.search(r'\*\*Antworten\*\*\s*\n(.*?)(?=\n\*\*|\Z)', section, re.DOTALL)

        if not q_block:
            continue

        questions = re.findall(r'\d+\.\s+(.+)', q_block.group(1))
        answers   = re.findall(r'\d+\.\s+(.+)', a_block.group(1)) if a_block else []

        for i, q in enumerate(questions):
            cards.append({
                "id":      f"{section_title}_{i}",
                "section": section_title,
                "q":       q.strip(),
                "a":       answers[i].strip() if i < len(answers) else "–",
            })
    return cards

def get_pipeline_status() -> dict:
    log_path = Path(PIPELINE_LOG)
    last_run = None
    last_ok  = None
    if log_path.exists():
        content = log_path.read_text(encoding="utf-8", errors="replace")
        runs = re.findall(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Pipeline gestartet', content)
        ends = re.findall(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) Pipeline beendet', content)
        last_run = runs[-1] if runs else None
        last_ok  = ends[-1] if ends else None
    return {"last_run": last_run, "last_ok": last_ok}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/courses")
def api_courses():
    return jsonify(get_courses())

@app.route("/api/files/<path:course_name>")
def api_files(course_name):
    d = COURSES_DIR / course_name
    if not d.is_dir():
        return jsonify([])
    return jsonify(list_files(d))

@app.route("/api/file-text/<path:course_name>/<path:filename>")
def api_file_text(course_name, filename):
    text = read_file_text(course_name, filename)
    return jsonify({"text": text})

@app.route("/api/file-raw/<path:course_name>/<path:filename>")
def api_file_raw(course_name, filename):
    path = COURSES_DIR / course_name / filename
    if not path.exists():
        return "Not found", 404
    return send_file(str(path))

@app.route("/api/summary/<path:course_name>")
def api_summary(course_name):
    path = COURSES_DIR / course_name / OUTPUT_FILENAME
    if not path.exists():
        return jsonify({"html": None, "md": None})
    md   = path.read_text(encoding="utf-8")
    html = markdown2.markdown(md, extras=["fenced-code-blocks", "tables"])
    return jsonify({"html": html, "md": md})

@app.route("/api/flashcards/<path:course_name>")
def api_flashcards(course_name):
    path = COURSES_DIR / course_name / OUTPUT_FILENAME
    if not path.exists():
        return jsonify([])
    md    = path.read_text(encoding="utf-8")
    cards = parse_flashcards(md)
    return jsonify(cards)

@app.route("/api/notes/<path:course_name>", methods=["GET", "POST"])
def api_notes(course_name):
    path = COURSES_DIR / course_name / NOTES_FILENAME
    if request.method == "POST":
        text = request.json.get("text", "")
        path.write_text(text, encoding="utf-8")
        return jsonify({"ok": True})
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return jsonify({"text": text})

@app.route("/api/progress/<path:course_name>", methods=["GET", "POST"])
def api_progress(course_name):
    prog = load_progress()
    if request.method == "POST":
        data = request.json
        prog[course_name] = data
        save_progress(prog)
        return jsonify({"ok": True})
    return jsonify(prog.get(course_name, {"total": 0, "known": 0, "cards": {}}))

@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    data  = request.json
    course = data.get("course", "")
    limit  = data.get("limit", 3)
    force  = data.get("force", False)
    files  = data.get("files", [])

    cmd = [PYTHON, SUMMARIZE_SCRIPT, "--course", course, "--limit", str(limit)]
    if force:
        cmd.append("--force")
    if files:
        cmd += ["--files"] + files

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return jsonify({"success": result.returncode == 0, "log": result.stdout + result.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "log": "Timeout nach 5 Minuten."})
    except Exception as e:
        return jsonify({"success": False, "log": str(e)})

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    try:
        result = subprocess.run([PYTHON, SCRAPER_SCRIPT], capture_output=True, text=True, timeout=600)
        return jsonify({"success": result.returncode == 0, "log": result.stdout + result.stderr})
    except Exception as e:
        return jsonify({"success": False, "log": str(e)})

@app.route("/api/pipeline-status")
def api_pipeline():
    return jsonify(get_pipeline_status())

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify([])
    results = []
    for d in sorted(COURSES_DIR.iterdir()):
        if not d.is_dir():
            continue
        path = d / OUTPUT_FILENAME
        if not path.exists():
            continue
        md = path.read_text(encoding="utf-8", errors="replace")
        if q in md.lower():
            # Find context around match
            idx = md.lower().find(q)
            snippet = md[max(0, idx-80):idx+120].replace('\n', ' ')
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            results.append({"course": d.name, "snippet": snippet})
    return jsonify(results)

# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stud.IP Dashboard</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:       #0e1016;
  --bg2:      #13151f;
  --bg3:      #1a1d2e;
  --bg4:      #21243a;
  --border:   #252840;
  --text:     #e2e8f0;
  --text2:    #94a3b8;
  --text3:    #475569;
  --blue:     #3b82f6;
  --blue2:    #1d4ed8;
  --green:    #22c55e;
  --yellow:   #eab308;
  --red:      #ef4444;
  --purple:   #a78bfa;
  --radius:   8px;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-size: 14px;
}

/* ── Topbar ── */
#topbar {
  height: 50px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 18px;
  gap: 14px;
  flex-shrink: 0;
  z-index: 10;
}
#topbar-logo { font-size: 15px; font-weight: 700; color: #fff; flex: 1; display: flex; align-items: center; gap: 8px; }
#topbar-logo span { color: var(--blue); }

#search-global {
  flex: 0 0 280px;
  padding: 7px 12px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 20px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  transition: border-color .15s;
}
#search-global:focus { border-color: var(--blue); }
#search-global::placeholder { color: var(--text3); }

.tbtn {
  padding: 6px 14px;
  border-radius: var(--radius);
  border: none;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: opacity .15s;
  white-space: nowrap;
}
.tbtn:hover { opacity: .85; }
.tbtn:disabled { opacity: .35; cursor: not-allowed; }
.btn-blue   { background: var(--blue); color: #fff; }
.btn-gray   { background: var(--bg4); color: var(--text2); }
.btn-green  { background: #166534; color: #86efac; }

/* ── Layout ── */
#layout { display: flex; flex: 1; overflow: hidden; }

/* ── Sidebar ── */
#sidebar {
  width: 250px; min-width: 140px; max-width: 480px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
}

#sidebar-top {
  padding: 10px;
  border-bottom: 1px solid var(--border);
}
#sidebar-search {
  width: 100%; padding: 7px 10px;
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: var(--radius); color: var(--text); font-size: 12px; outline: none;
}
#sidebar-search:focus { border-color: var(--blue); }
#sidebar-search::placeholder { color: var(--text3); }

.sidebar-section-label {
  padding: 8px 12px 4px;
  font-size: 10px; font-weight: 600;
  color: var(--text3); text-transform: uppercase; letter-spacing: .08em;
}

#course-list { overflow-y: auto; flex: 1; padding: 4px 6px; }

.citem {
  padding: 8px 10px; border-radius: 7px;
  cursor: pointer; display: flex; align-items: center; gap: 8px;
  margin-bottom: 1px; transition: background .1s; position: relative;
}
.citem:hover  { background: var(--bg3); }
.citem.active { background: #1a2a4a; }

.citem-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot-ok     { background: var(--green); }
.dot-missing{ background: var(--text3); }

.citem-body { flex: 1; min-width: 0; }
.citem-name { font-size: 12px; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.citem.active .citem-name { color: #fff; }
.citem-meta { font-size: 10px; color: var(--text3); display: flex; gap: 8px; margin-top: 2px; }

.progress-mini {
  height: 2px; background: var(--border); border-radius: 2px; margin-top: 4px;
}
.progress-mini-fill { height: 100%; background: var(--blue); border-radius: 2px; transition: width .3s; }

/* Favorite star button */
.fav-btn {
  background: none; border: none; cursor: pointer;
  font-size: 13px; padding: 2px 4px; flex-shrink: 0;
  opacity: 0.35; transition: opacity .15s, transform .1s;
  line-height: 1;
}
.fav-btn:hover { opacity: 1; transform: scale(1.2); }
.fav-btn.is-fav { opacity: 1; }

/* ── Resize divider ── */
.resize-divider {
  width: 6px;
  background: transparent;
  cursor: col-resize;
  flex-shrink: 0;
  position: relative;
  z-index: 5;
  transition: background .15s;
}
.resize-divider:hover,
.resize-divider.dragging { background: var(--blue); }

.resize-divider-v {
  height: 6px;
  width: 100%;
  background: transparent;
  cursor: row-resize;
  flex-shrink: 0;
  position: relative;
  z-index: 5;
  transition: background .15s;
}
.resize-divider-v:hover,
.resize-divider-v.dragging { background: var(--blue); }

/* ── Main ── */
#main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }

/* ── Tabs ── */
#tabs {
  display: flex; align-items: center;
  padding: 0 20px; gap: 2px;
  background: var(--bg2); border-bottom: 1px solid var(--border);
  height: 44px; flex-shrink: 0;
}
.tab {
  padding: 6px 14px; border-radius: 6px; cursor: pointer;
  font-size: 13px; font-weight: 500; color: var(--text3);
  transition: all .15s; border: none; background: none;
}
.tab:hover { color: var(--text2); background: var(--bg3); }
.tab.active { color: #fff; background: var(--bg4); }
.tab-spacer { flex: 1; }
#tabs-course-label {
  font-size: 11px; color: var(--text3); font-weight: 400;
  max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ── Content ── */
#content { flex: 1; overflow: hidden; position: relative; }

.panel { position: absolute; inset: 0; overflow-y: auto; display: none; padding: 28px 36px; }
.panel.active { display: block; }

/* Home panel */
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }
.stat-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 20px;
}
.stat-label { font-size: 11px; color: var(--text3); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 8px; }
.stat-value { font-size: 28px; font-weight: 700; color: #fff; }
.stat-sub   { font-size: 12px; color: var(--text3); margin-top: 4px; }

.section-title { font-size: 13px; font-weight: 600; color: var(--text2); margin-bottom: 12px; text-transform: uppercase; letter-spacing: .05em; }

.pipeline-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 20px; display: flex; align-items: center; gap: 14px; margin-bottom: 24px;
}
.pipeline-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }

.recent-list { display: flex; flex-direction: column; gap: 8px; }
.recent-item {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 12px;
  transition: border-color .15s;
}
.recent-item:hover { border-color: var(--blue); }
.recent-item-name { flex: 1; font-size: 13px; color: var(--text); }
.recent-item-meta { font-size: 11px; color: var(--text3); }

/* Files panel */
.files-layout { display: flex; height: 100%; overflow: hidden; }
.files-list-col { width: 260px; min-width: 120px; max-width: 500px; flex-shrink: 0; overflow-y: auto; padding-right: 4px; }
.files-preview-col { flex: 1; min-width: 0; overflow: hidden; }

.file-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: var(--radius); cursor: pointer; transition: background .1s;
  border: 1px solid transparent;
}
.file-item:hover { background: var(--bg3); }
.file-item.active { background: var(--bg3); border-color: var(--blue); }
.file-item input[type=checkbox] { accent-color: var(--blue); flex-shrink: 0; }
.file-icon { font-size: 16px; flex-shrink: 0; }
.file-name { font-size: 12px; color: var(--text2); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.file-actions { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.limit-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text3); }
.limit-input {
  width: 55px; padding: 5px 8px; background: var(--bg3);
  border: 1px solid var(--border); border-radius: 5px;
  color: var(--text); font-size: 12px; text-align: center;
}

.preview-box {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
  height: 100%; overflow: hidden; display: flex; flex-direction: column;
}
.preview-header {
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  font-size: 12px; color: var(--text3); background: var(--bg3);
  border-radius: 10px 10px 0 0; flex-shrink: 0;
}
.preview-body {
  flex: 1; overflow-y: auto; padding: 20px;
  font-family: "SF Mono", monospace; font-size: 12px;
  color: var(--text2); line-height: 1.7; white-space: pre-wrap; word-break: break-word;
}
.preview-body.pdf-wrap { padding: 0; }
.preview-body iframe { width: 100%; height: 100%; border: none; }
.preview-placeholder {
  height: 100%; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 10px; color: var(--text3);
}
.preview-placeholder .icon { font-size: 36px; }

/* Summary panel */
.summary-toolbar {
  display: flex; align-items: center; gap: 10px; margin-bottom: 20px;
  padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
.md-content { max-width: 760px; }
.md-content h1 { font-size: 22px; color: #f1f5f9; margin-bottom: 6px; line-height: 1.3; }
.md-content h2 { font-size: 17px; color: #93c5fd; margin: 28px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.md-content h3 { font-size: 14px; color: #a5b4fc; margin: 18px 0 6px; }
.md-content p  { color: var(--text2); line-height: 1.75; margin-bottom: 10px; }
.md-content ul, .md-content ol { color: var(--text2); line-height: 1.8; margin: 6px 0 12px 22px; }
.md-content li { margin-bottom: 4px; }
.md-content strong { color: var(--text); }
.md-content em { color: var(--text3); }
.md-content hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
.md-content code { background: var(--bg3); padding: 2px 6px; border-radius: 4px; font-size: 12px; color: #86efac; }
.md-content blockquote { border-left: 3px solid var(--blue); padding-left: 14px; color: var(--text3); margin: 10px 0; }
.md-content table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
.md-content th { background: var(--bg3); padding: 8px 12px; text-align: left; color: #93c5fd; border: 1px solid var(--border); }
.md-content td { padding: 7px 12px; border: 1px solid var(--border); color: var(--text2); }

/* Flashcard panel */
.flash-layout { max-width: 680px; margin: 0 auto; }
.flash-progress-bar { background: var(--border); border-radius: 10px; height: 6px; margin-bottom: 24px; overflow: hidden; }
.flash-progress-fill { height: 100%; background: var(--blue); border-radius: 10px; transition: width .4s; }
.flash-meta { font-size: 12px; color: var(--text3); text-align: center; margin-bottom: 20px; }

.flash-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 14px; padding: 32px; text-align: center;
  min-height: 220px; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 14px;
  margin-bottom: 20px;
}
.flash-section { font-size: 11px; color: var(--text3); text-transform: uppercase; letter-spacing: .05em; }
.flash-question { font-size: 18px; color: #fff; line-height: 1.5; font-weight: 500; }
.flash-answer {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 24px;
  font-size: 14px; color: var(--text2); line-height: 1.6;
  display: none; text-align: left; width: 100%;
}

.flash-btns { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
.flash-btn {
  padding: 10px 24px; border-radius: var(--radius); border: none;
  cursor: pointer; font-size: 13px; font-weight: 600; transition: opacity .15s;
}
.flash-btn:hover { opacity: .85; }
.fb-reveal { background: var(--bg4); color: var(--text); }
.fb-known   { background: #166534; color: #86efac; }
.fb-unknown { background: #7f1d1d; color: #fca5a5; }

.flash-done {
  text-align: center; padding: 40px;
  display: none; flex-direction: column; align-items: center; gap: 14px;
}
.flash-done .big-icon { font-size: 56px; }
.flash-done h2 { font-size: 22px; color: #fff; }
.flash-done p  { color: var(--text3); font-size: 14px; }

/* flash keyboard hint */
.flash-kbd-hint {
  text-align: center; font-size: 11px; color: var(--text3); margin-top: 8px;
}
.flash-kbd-hint kbd {
  background: var(--bg3); border: 1px solid var(--border); border-radius: 4px;
  padding: 1px 5px; font-size: 10px; font-family: "SF Mono", monospace;
}

/* Notes panel */
.notes-panel { display: flex; flex-direction: column; height: calc(100vh - 150px); }
.notes-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
#notes-editor {
  flex: 1; background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 18px; color: var(--text2);
  font-size: 14px; line-height: 1.75; resize: none; outline: none;
  font-family: inherit; transition: border-color .15s;
}
#notes-editor:focus { border-color: var(--blue); }
#notes-saved { font-size: 12px; display: none; }

/* Search results */
.search-results { padding: 20px 36px; }
.search-result {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 18px; margin-bottom: 10px;
  cursor: pointer; transition: border-color .15s;
}
.search-result:hover { border-color: var(--blue); }
.search-result-course { font-size: 12px; color: var(--blue); margin-bottom: 5px; font-weight: 600; }
.search-result-snippet { font-size: 13px; color: var(--text2); line-height: 1.6; }
.search-result-snippet mark { background: rgba(59,130,246,.25); color: #93c5fd; border-radius: 3px; padding: 0 2px; }
.search-empty { text-align: center; padding: 40px; color: var(--text3); }

/* Log */
#log-box {
  position: fixed; bottom: 16px; right: 16px; width: 420px;
  background: #0d0f18; border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; font-family: "SF Mono", monospace; font-size: 11px;
  color: #4ade80; white-space: pre-wrap; max-height: 200px; overflow-y: auto;
  display: none; z-index: 100; box-shadow: 0 8px 30px rgba(0,0,0,.5);
}
#log-close { position: absolute; top: 8px; right: 10px; cursor: pointer; color: var(--text3); font-size: 14px; }

/* Spinner */
.spin {
  display: inline-block; width: 14px; height: 14px;
  border: 2px solid var(--border); border-top-color: var(--blue);
  border-radius: 50%; animation: spin .65s linear infinite; vertical-align: middle;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Empty state */
.empty-state {
  height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  color: var(--text3); gap: 12px; text-align: center;
}
.empty-state .icon { font-size: 52px; }
.empty-state h3 { font-size: 16px; color: var(--text2); }
.empty-state p { font-size: 13px; max-width: 300px; line-height: 1.6; }

/* Scrollbar */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }

/* files panel inner layout fix */
#panel-files { padding: 16px 20px; }
</style>
</head>
<body>

<!-- Topbar -->
<div id="topbar">
  <div id="topbar-logo">📚 <span>Stud.IP</span> Dashboard</div>
  <input id="search-global" type="text" placeholder="🔍  Suche in allen Zusammenfassungen…" oninput="handleGlobalSearch(event)">
  <button class="tbtn btn-gray" onclick="goHome()">Übersicht</button>
  <button class="tbtn btn-blue" id="scrape-btn" onclick="runScraper()">↓ Neue Dateien</button>
</div>

<!-- Layout -->
<div id="layout">

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-top">
      <input id="sidebar-search" type="text" placeholder="Kurs suchen…" oninput="filterSidebar()">
    </div>
    <div id="course-list"></div>
  </div>

  <!-- Sidebar / Main resize divider -->
  <div class="resize-divider" id="divider-sidebar" title="Ziehen zum Anpassen"></div>

  <!-- Main -->
  <div id="main">

    <!-- Tabs (hidden on home) -->
    <div id="tabs" style="display:none">
      <button class="tab active" data-tab="files"   onclick="switchTab('files')">📁 Dateien</button>
      <button class="tab"        data-tab="summary" onclick="switchTab('summary')">📄 Zusammenfassung</button>
      <button class="tab"        data-tab="learn"   onclick="switchTab('learn')">🧠 Lernen</button>
      <button class="tab"        data-tab="notes"   onclick="switchTab('notes')">✏️ Notizen</button>
      <div class="tab-spacer"></div>
      <span id="tab-spinner" style="display:none" class="spin"></span>
      <span id="tabs-course-label"></span>
    </div>

    <!-- Content panels -->
    <div id="content">

      <!-- Home -->
      <div class="panel active" id="panel-home">
        <div class="stats-grid" id="stats-grid"></div>
        <div id="pipeline-card-wrap"></div>
        <div class="section-title">Zuletzt aktualisiert</div>
        <div class="recent-list" id="recent-list"></div>
      </div>

      <!-- Files -->
      <div class="panel" id="panel-files">
        <div class="files-layout" id="files-layout">
          <div class="files-list-col" id="files-list-col">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
              <span style="font-size:12px;font-weight:600;color:var(--text3);">DATEIEN</span>
              <button style="font-size:11px;color:var(--blue);background:none;border:none;cursor:pointer;" onclick="toggleAllFiles()">Alle</button>
            </div>
            <div id="file-list"></div>
            <div class="file-actions">
              <div class="limit-row">
                Max. Dateien: <input class="limit-input" id="limit-input" type="number" value="3" min="1" max="50">
              </div>
              <button class="tbtn btn-blue" style="width:100%" onclick="generateSummary(false)">Zusammenfassen</button>
              <button class="tbtn btn-gray" style="width:100%" onclick="generateSummary(true)">↺ Neu generieren</button>
            </div>
          </div>
          <!-- Files / Preview resize divider -->
          <div class="resize-divider" id="divider-files" title="Ziehen zum Anpassen"></div>
          <div class="files-preview-col" id="files-preview-col">
            <div class="preview-box">
              <div class="preview-header" id="preview-header">Datei auswählen zum Anzeigen</div>
              <div class="preview-body" id="preview-body">
                <div class="preview-placeholder">
                  <div class="icon">👆</div>
                  <div>Datei anklicken</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Summary -->
      <div class="panel" id="panel-summary">
        <div id="summary-body">
          <div class="empty-state">
            <div class="icon">📄</div>
            <h3>Keine Zusammenfassung</h3>
            <p>Gehe zu "Dateien" und klicke "Zusammenfassen"</p>
          </div>
        </div>
      </div>

      <!-- Learn / Flashcards -->
      <div class="panel" id="panel-learn">
        <div id="learn-body">
          <div class="empty-state">
            <div class="icon">🧠</div>
            <h3>Keine Lernkarten</h3>
            <p>Erstelle zuerst eine Zusammenfassung</p>
          </div>
        </div>
      </div>

      <!-- Notes -->
      <div class="panel" id="panel-notes">
        <div class="notes-panel">
          <div class="notes-toolbar">
            <span style="font-size:13px;font-weight:600;color:var(--text2);">Notizen</span>
            <div style="flex:1"></div>
            <span id="notes-saved"></span>
            <button class="tbtn btn-blue" onclick="saveNotes()">Speichern</button>
          </div>
          <textarea id="notes-editor" placeholder="Eigene Notizen, Fragen, Zusammenhänge…"></textarea>
        </div>
      </div>

      <!-- Search results overlay -->
      <div class="panel" id="panel-search">
        <div id="search-results-body"></div>
      </div>

    </div>
  </div>
</div>

<!-- Log box -->
<div id="log-box">
  <span id="log-close" onclick="document.getElementById('log-box').style.display='none'">✕</span>
  <div id="log-content"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let allCourses   = [];
let activeCourse = null;
let activeTab    = 'home';
let flashState   = { cards: [], index: 0, revealed: false, progress: {} };
let notesSaveTimer = null;
let allFilesChecked = true;

// Favorites (stored in localStorage)
function getFavorites() {
  try { return JSON.parse(localStorage.getItem('fav_courses') || '[]'); } catch { return []; }
}
function setFavorites(arr) {
  localStorage.setItem('fav_courses', JSON.stringify(arr));
}
function isFavorite(name) { return getFavorites().includes(name); }
function toggleFavorite(name) {
  let favs = getFavorites();
  if (favs.includes(name)) {
    favs = favs.filter(f => f !== name);
  } else {
    favs.push(name);
  }
  setFavorites(favs);
}

// ═══════════════════════════════════════════════════════════════════════════
// Boot
// ═══════════════════════════════════════════════════════════════════════════
async function boot() {
  const [courses, pipeline] = await Promise.all([
    fetch('/api/courses').then(r => r.json()),
    fetch('/api/pipeline-status').then(r => r.json()),
  ]);
  allCourses = courses;
  renderSidebar(allCourses);
  renderHome(courses, pipeline);
  initResizeDividers();
}

// ═══════════════════════════════════════════════════════════════════════════
// Sidebar
// ═══════════════════════════════════════════════════════════════════════════
function renderSidebar(courses) {
  const el = document.getElementById('course-list');
  const favs = getFavorites();
  const favCourses = courses.filter(c => favs.includes(c.name));
  const restCourses = courses.filter(c => !favs.includes(c.name));

  function courseHTML(c) {
    const pct = c.progress.total ? Math.round(c.progress.known / c.progress.total * 100) : 0;
    const fav = isFavorite(c.name);
    return `
    <div class="citem ${activeCourse === c.name ? 'active' : ''}" data-course="${esc(c.name)}" onclick="selectCourseFromEl(this)">
      <div class="citem-dot ${c.has_summary ? 'dot-ok' : 'dot-missing'}"></div>
      <div class="citem-body">
        <div class="citem-name">${esc(c.name)}</div>
        <div class="citem-meta">
          <span>${c.file_count} Datei${c.file_count !== 1 ? 'en' : ''}</span>
          ${c.has_summary ? `<span style="color:var(--green)">✓ Zusammenfassung</span>` : ''}
        </div>
        ${c.progress.total ? `<div class="progress-mini"><div class="progress-mini-fill" style="width:${pct}%"></div></div>` : ''}
      </div>
      <button class="fav-btn ${fav ? 'is-fav' : ''}" title="${fav ? 'Favorit entfernen' : 'Als Favorit markieren'}"
        onclick="event.stopPropagation(); handleFavClick(this, '${esc(c.name)}')">⭐</button>
    </div>`;
  }

  let html = '';
  if (favCourses.length) {
    html += `<div class="sidebar-section-label">⭐ Favoriten</div>`;
    html += favCourses.map(courseHTML).join('');
    html += `<div class="sidebar-section-label">Kurse</div>`;
  } else {
    html += `<div class="sidebar-section-label">Kurse</div>`;
  }
  html += restCourses.map(courseHTML).join('');

  el.innerHTML = html;
}

function selectCourseFromEl(el) {
  selectCourse(el.dataset.course);
}

function handleFavClick(btn, name) {
  toggleFavorite(name);
  // Re-render sidebar with current filter
  const q = document.getElementById('sidebar-search').value.toLowerCase();
  renderSidebar(allCourses.filter(c => c.name.toLowerCase().includes(q)));
}

function filterSidebar() {
  const q = document.getElementById('sidebar-search').value.toLowerCase();
  renderSidebar(allCourses.filter(c => c.name.toLowerCase().includes(q)));
}

// ═══════════════════════════════════════════════════════════════════════════
// Home
// ═══════════════════════════════════════════════════════════════════════════
function renderHome(courses, pipeline) {
  const total      = courses.length;
  const withSum    = courses.filter(c => c.has_summary).length;
  const totalFiles = courses.reduce((a, c) => a + c.file_count, 0);
  const totalQ     = courses.reduce((a, c) => a + c.progress.total, 0);

  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Kurse</div>
      <div class="stat-value">${total}</div>
      <div class="stat-sub">${withSum} mit Zusammenfassung</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Dateien gesamt</div>
      <div class="stat-value">${totalFiles}</div>
      <div class="stat-sub">in allen Kursen</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Lernkarten</div>
      <div class="stat-value">${totalQ}</div>
      <div class="stat-sub">aus Zusammenfassungen</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Ausstehend</div>
      <div class="stat-value">${total - withSum}</div>
      <div class="stat-sub">Kurse ohne Zusammenfassung</div>
    </div>`;

  const pOk = pipeline.last_ok;
  document.getElementById('pipeline-card-wrap').innerHTML = `
    <div class="pipeline-card" style="margin-bottom:24px">
      <div class="pipeline-dot" style="background:${pOk ? 'var(--green)' : 'var(--text3)'}"></div>
      <div>
        <div style="font-size:13px;font-weight:600;color:#fff">Automatische Pipeline</div>
        <div style="font-size:12px;color:var(--text3);margin-top:3px">
          ${pOk ? `Letzter Lauf: ${pOk}` : 'Noch nie gelaufen'}
          &nbsp;·&nbsp; Montag + Donnerstag 08:00
        </div>
      </div>
      <div style="flex:1"></div>
      <button class="tbtn btn-gray" onclick="runScraper()">Jetzt ausführen</button>
    </div>`;

  const recent = [...courses]
    .filter(c => c.summary_age)
    .sort((a, b) => b.summary_age - a.summary_age)
    .slice(0, 8);

  document.getElementById('recent-list').innerHTML = recent.length ? recent.map(c => `
    <div class="recent-item" data-course="${esc(c.name)}" onclick="selectCourseFromEl(this)">
      <span style="font-size:20px">${c.has_notes ? '📝' : '📚'}</span>
      <div class="recent-item-name">${esc(c.name)}</div>
      <div class="recent-item-meta">${c.file_count} Dateien</div>
      ${c.progress.total ? `
        <div style="font-size:11px;color:var(--blue)">
          ${c.progress.known}/${c.progress.total} Karten
        </div>` : ''}
    </div>
  `).join('') : '<div style="color:var(--text3);font-size:13px">Noch keine Zusammenfassungen erstellt</div>';
}

function goHome() {
  activeCourse = null;
  document.getElementById('tabs').style.display = 'none';
  showPanel('home');
  renderSidebar(allCourses);
}

// ═══════════════════════════════════════════════════════════════════════════
// Course selection
// ═══════════════════════════════════════════════════════════════════════════
async function selectCourse(name) {
  activeCourse = name;
  renderSidebar(allCourses.filter(c =>
    c.name.toLowerCase().includes(document.getElementById('sidebar-search').value.toLowerCase())
  ));
  document.getElementById('tabs').style.display = 'flex';
  document.getElementById('tabs-course-label').textContent = name;
  switchTab('files');
  loadFiles();
}

// ═══════════════════════════════════════════════════════════════════════════
// Tabs
// ═══════════════════════════════════════════════════════════════════════════
function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  showPanel(tab);

  if (tab === 'summary') loadSummary();
  if (tab === 'learn')   loadFlashcards();
  if (tab === 'notes')   loadNotes();
}

function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
}

// ═══════════════════════════════════════════════════════════════════════════
// Files tab
// ═══════════════════════════════════════════════════════════════════════════
async function loadFiles() {
  const files = await fetch(`/api/files/${enc(activeCourse)}`).then(r => r.json());
  const el = document.getElementById('file-list');
  if (!files.length) {
    el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px">Keine Dateien</div>';
    return;
  }
  el.innerHTML = files.map(f => `
    <div class="file-item" id="fi-${CSS.escape(f)}" data-filename="${esc(f)}" onclick="previewFileFromEl(this)">
      <input type="checkbox" name="file" value="${esc(f)}" checked onclick="event.stopPropagation()">
      <span class="file-icon">${fileIcon(f)}</span>
      <span class="file-name" title="${esc(f)}">${esc(f)}</span>
    </div>
  `).join('');
}

function previewFileFromEl(el) {
  previewFile(el.dataset.filename);
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  return {pdf:'📕', docx:'📘', pptx:'📙', txt:'📃', md:'📝'}[ext] || '📄';
}

async function previewFile(filename) {
  document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
  // Find by data-filename
  const match = document.querySelector(`.file-item[data-filename="${CSS.escape(filename)}"]`);
  if (match) match.classList.add('active');

  const ext = filename.split('.').pop().toLowerCase();
  const header = document.getElementById('preview-header');
  const body   = document.getElementById('preview-body');

  header.textContent = filename;
  body.innerHTML = '<div class="preview-placeholder"><div class="icon">⏳</div><div>Lade…</div></div>';
  body.className = 'preview-body';

  if (ext === 'pdf') {
    body.className = 'preview-body pdf-wrap';
    body.innerHTML = `<iframe src="/api/file-raw/${enc(activeCourse)}/${enc(filename)}"></iframe>`;
  } else {
    const data = await fetch(`/api/file-text/${enc(activeCourse)}/${enc(filename)}`).then(r => r.json());
    body.innerHTML = esc(data.text || '(Kein Text lesbar)');
  }
}

function toggleAllFiles() {
  allFilesChecked = !allFilesChecked;
  document.querySelectorAll('input[name="file"]').forEach(cb => cb.checked = allFilesChecked);
}

function getSelectedFiles() {
  return [...document.querySelectorAll('input[name="file"]:checked')].map(cb => cb.value);
}

// ═══════════════════════════════════════════════════════════════════════════
// Summary tab
// ═══════════════════════════════════════════════════════════════════════════
async function loadSummary() {
  const data = await fetch(`/api/summary/${enc(activeCourse)}`).then(r => r.json());
  const el = document.getElementById('summary-body');
  if (data.html) {
    el.innerHTML = `
      <div class="summary-toolbar">
        <span style="font-size:13px;font-weight:600;color:var(--text2);">Zusammenfassung</span>
        <div style="flex:1"></div>
        <button class="tbtn btn-gray" onclick="switchTab('files')">Neu erstellen</button>
      </div>
      <div class="md-content">${data.html}</div>`;
  } else {
    el.innerHTML = `
      <div class="empty-state">
        <div class="icon">📄</div>
        <h3>Keine Zusammenfassung</h3>
        <p>Gehe zu "Dateien", wähle Dateien aus und klicke "Zusammenfassen"</p>
        <button class="tbtn btn-blue" onclick="switchTab('files')">Zu Dateien →</button>
      </div>`;
  }
}

async function generateSummary(force) {
  const files = getSelectedFiles();
  const limit = parseInt(document.getElementById('limit-input').value) || 3;
  setLoading(true);
  logShow(`Generiere Zusammenfassung für "${activeCourse}"…\n`);

  const res  = await fetch('/api/summarize', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ course: activeCourse, limit, force, files })
  });
  const data = await res.json();
  logAppend(data.log || '');
  setLoading(false);

  if (data.success) {
    logAppend('\n✅ Fertig!');
    // Refresh course list
    allCourses = await fetch('/api/courses').then(r => r.json());
    renderSidebar(allCourses);
    switchTab('summary');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcards
// ═══════════════════════════════════════════════════════════════════════════
async function loadFlashcards() {
  const [cards, prog] = await Promise.all([
    fetch(`/api/flashcards/${enc(activeCourse)}`).then(r => r.json()),
    fetch(`/api/progress/${enc(activeCourse)}`).then(r => r.json()),
  ]);

  if (!cards.length) {
    document.getElementById('learn-body').innerHTML = `
      <div class="empty-state">
        <div class="icon">🧠</div>
        <h3>Keine Lernkarten</h3>
        <p>Erstelle zuerst eine Zusammenfassung — Trainingsfragen werden automatisch als Karten erkannt.</p>
        <button class="tbtn btn-blue" onclick="switchTab('files')">Zusammenfassung erstellen →</button>
      </div>`;
    return;
  }

  flashState = { cards, index: 0, revealed: false, progress: prog.cards || {} };
  renderFlash();
}

function renderFlash() {
  const { cards, index, revealed, progress } = flashState;
  const known   = Object.values(progress).filter(v => v === 'known').length;
  const unknown = Object.values(progress).filter(v => v === 'unknown').length;
  const done    = known + unknown;
  const pct     = cards.length ? Math.round(done / cards.length * 100) : 0;

  if (index >= cards.length) {
    document.getElementById('learn-body').innerHTML = `
      <div class="flash-layout">
        <div class="flash-done" style="display:flex">
          <div class="big-icon">🎉</div>
          <h2>Alle Karten durch!</h2>
          <p>${known} gewusst · ${unknown} nicht gewusst · ${cards.length} gesamt</p>
          <div style="display:flex;gap:10px;margin-top:10px">
            <button class="tbtn btn-gray" onclick="restartFlash()">Nochmal</button>
            <button class="tbtn btn-blue" onclick="restartFlashUnknown()">Nur falsche</button>
          </div>
        </div>
      </div>`;
    // Save progress
    fetch(`/api/progress/${enc(activeCourse)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ total: cards.length, known, cards: progress })
    });
    allCourses = allCourses.map(c => c.name === activeCourse ? {...c, progress: {total: cards.length, known}} : c);
    renderSidebar(allCourses);
    return;
  }

  const card = cards[index];
  document.getElementById('learn-body').innerHTML = `
    <div class="flash-layout">
      <div class="flash-progress-bar">
        <div class="flash-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="flash-meta">${index + 1} / ${cards.length} &nbsp;·&nbsp; ${known} gewusst · ${unknown} nicht gewusst</div>
      <div class="flash-card">
        <div class="flash-section">${esc(card.section)}</div>
        <div class="flash-question">${esc(card.q)}</div>
        <div class="flash-answer" id="flash-answer">${esc(card.a)}</div>
      </div>
      <div class="flash-btns" id="flash-btns">
        <button class="flash-btn fb-reveal" onclick="revealFlash()">Antwort zeigen</button>
      </div>
      <div class="flash-kbd-hint">
        <kbd>Space</kbd> Antwort &nbsp; <kbd>→</kbd>/<kbd>k</kbd> Gewusst &nbsp; <kbd>←</kbd>/<kbd>u</kbd> Nicht gewusst &nbsp; <kbd>Esc</kbd> Übersicht
      </div>
    </div>`;
}

function revealFlash() {
  document.getElementById('flash-answer').style.display = 'block';
  document.getElementById('flash-btns').innerHTML = `
    <button class="flash-btn fb-unknown" onclick="rateFlash('unknown')">✗ Nicht gewusst</button>
    <button class="flash-btn fb-known"   onclick="rateFlash('known')">✓ Gewusst</button>`;
  flashState.revealed = true;
}

function rateFlash(rating) {
  const card = flashState.cards[flashState.index];
  flashState.progress[card.id] = rating;
  flashState.index++;
  flashState.revealed = false;

  // Save progress immediately after each card
  const known = Object.values(flashState.progress).filter(v => v === 'known').length;
  fetch(`/api/progress/${enc(activeCourse)}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ total: flashState.cards.length, known, cards: flashState.progress })
  });

  renderFlash();
}

function restartFlash() {
  flashState.index = 0;
  flashState.progress = {};
  renderFlash();
}

function restartFlashUnknown() {
  const unknown = flashState.cards.filter(c => flashState.progress[c.id] !== 'known');
  flashState = { cards: unknown, index: 0, revealed: false, progress: {} };
  renderFlash();
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcard keyboard shortcuts
// ═══════════════════════════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (activeTab !== 'learn') return;
  // Don't fire if user is typing in an input/textarea
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  if (e.key === ' ' || e.code === 'Space') {
    e.preventDefault();
    if (!flashState.revealed && flashState.index < flashState.cards.length) {
      revealFlash();
    }
  } else if (e.key === 'ArrowRight' || e.key === 'k') {
    e.preventDefault();
    if (flashState.revealed && flashState.index < flashState.cards.length) {
      rateFlash('known');
    }
  } else if (e.key === 'ArrowLeft' || e.key === 'u') {
    e.preventDefault();
    if (flashState.revealed && flashState.index < flashState.cards.length) {
      rateFlash('unknown');
    }
  } else if (e.key === 'Escape') {
    goHome();
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Notes
// ═══════════════════════════════════════════════════════════════════════════
async function loadNotes() {
  const data = await fetch(`/api/notes/${enc(activeCourse)}`).then(r => r.json());
  const editor = document.getElementById('notes-editor');
  editor.value = data.text || '';
  // Reset unsaved indicator
  const saved = document.getElementById('notes-saved');
  saved.style.display = 'none';
}

async function saveNotes() {
  const text = document.getElementById('notes-editor').value;
  await fetch(`/api/notes/${enc(activeCourse)}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ text })
  });
  const el = document.getElementById('notes-saved');
  el.textContent = '✓ Gespeichert';
  el.style.color = 'var(--green)';
  el.style.display = 'inline';
  setTimeout(() => { el.style.display = 'none'; }, 2000);
  allCourses = allCourses.map(c => c.name === activeCourse ? {...c, has_notes: true} : c);
}

// Auto-save notes + unsaved indicator
document.addEventListener('input', e => {
  if (e.target.id === 'notes-editor') {
    const el = document.getElementById('notes-saved');
    el.textContent = '● Ungespeichert';
    el.style.color = '#f97316'; // orange
    el.style.display = 'inline';
    clearTimeout(notesSaveTimer);
    notesSaveTimer = setTimeout(saveNotes, 1500);
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Global search
// ═══════════════════════════════════════════════════════════════════════════
let searchTimer = null;
function handleGlobalSearch(e) {
  const q = e.target.value.trim();
  clearTimeout(searchTimer);
  if (!q) { if (activeCourse) switchTab(activeTab); else goHome(); return; }
  searchTimer = setTimeout(() => doSearch(q), 300);
}

async function doSearch(q) {
  showPanel('search');
  document.getElementById('tabs').style.display = 'none';
  const results = await fetch(`/api/search?q=${encodeURIComponent(q)}`).then(r => r.json());
  const el = document.getElementById('search-results-body');
  if (!results.length) {
    el.innerHTML = `<div class="search-empty">Keine Ergebnisse für „${esc(q)}"</div>`;
    return;
  }
  // Fix: escape regex special chars before building RegExp
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const highlighted = results.map(r => {
    const snippet = r.snippet.replace(new RegExp(escaped, 'gi'), m => `<mark>${m}</mark>`);
    return `
      <div class="search-result" data-course="${esc(r.course)}" onclick="selectCourseFromEl(this);switchTab('summary')">
        <div class="search-result-course">${esc(r.course)}</div>
        <div class="search-result-snippet">…${snippet}…</div>
      </div>`;
  }).join('');
  el.innerHTML = `<div style="font-size:12px;color:var(--text3);margin-bottom:16px">${results.length} Treffer für „${esc(q)}"</div>${highlighted}`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Scraper
// ═══════════════════════════════════════════════════════════════════════════
async function runScraper() {
  setLoading(true);
  logShow('Lade neue Dateien von Stud.IP…\n');
  const res  = await fetch('/api/scrape', { method: 'POST' });
  const data = await res.json();
  logAppend(data.log || '');
  setLoading(false);
  if (data.success) {
    logAppend('\n✅ Fertig!');
    allCourses = await fetch('/api/courses').then(r => r.json());
    renderSidebar(allCourses);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Resizable dividers
// ═══════════════════════════════════════════════════════════════════════════
function initResizeDividers() {
  // Restore saved sizes
  const savedSidebar = localStorage.getItem('sidebar_width');
  if (savedSidebar) {
    document.getElementById('sidebar').style.width = savedSidebar + 'px';
  }
  const savedFilesCol = localStorage.getItem('files_col_width');
  if (savedFilesCol) {
    document.getElementById('files-list-col').style.width = savedFilesCol + 'px';
  }

  setupColResize(
    document.getElementById('divider-sidebar'),
    document.getElementById('sidebar'),
    140, 480,
    'sidebar_width'
  );

  setupColResize(
    document.getElementById('divider-files'),
    document.getElementById('files-list-col'),
    120, 500,
    'files_col_width'
  );
}

function setupColResize(divider, targetEl, minW, maxW, storageKey) {
  let startX, startW;

  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = targetEl.getBoundingClientRect().width;
    divider.classList.add('dragging');

    function onMove(e) {
      const delta = e.clientX - startX;
      const newW  = Math.min(maxW, Math.max(minW, startW + delta));
      targetEl.style.width = newW + 'px';
      // For sidebar, update min-width too to prevent flex collapse
      if (storageKey === 'sidebar_width') {
        targetEl.style.minWidth = newW + 'px';
      }
    }

    function onUp() {
      divider.classList.remove('dragging');
      localStorage.setItem(storageKey, parseInt(targetEl.style.width));
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════════
function setLoading(on) {
  document.getElementById('tab-spinner').style.display = on ? 'inline-block' : 'none';
  document.querySelectorAll('.tbtn, .flash-btn').forEach(b => b.disabled = on);
}

function logShow(text) {
  const box = document.getElementById('log-box');
  box.style.display = 'block';
  document.getElementById('log-content').textContent = text;
  box.scrollTop = box.scrollHeight;
}
function logAppend(text) {
  const el = document.getElementById('log-content');
  el.textContent += text;
  document.getElementById('log-box').scrollTop = 9999;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function enc(s) { return encodeURIComponent(s); }

boot();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print("Dashboard → http://localhost:5001")
    app.run(debug=True, port=5001)
