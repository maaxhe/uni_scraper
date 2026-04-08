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

def _course_info(rel_path: str, d: Path, progress: dict) -> dict:
    """Build info dict for a single course directory."""
    files = list_files(d)
    summary_path = d / OUTPUT_FILENAME
    notes_path   = d / NOTES_FILENAME
    p = progress.get(rel_path, progress.get(d.name, {}))
    summary_mtime = int(summary_path.stat().st_mtime) if summary_path.exists() else None
    # Count files newer than the summary
    new_files = 0
    if summary_mtime:
        for fname in files:
            fpath = d / fname
            if fpath.exists() and int(fpath.stat().st_mtime) > summary_mtime:
                new_files += 1
    return {
        "name":        d.name,
        "path":        rel_path,
        "has_summary": summary_path.exists(),
        "has_notes":   notes_path.exists(),
        "file_count":  len(files),
        "new_files":   new_files,
        "summary_age": summary_mtime,
        "progress":    {
            "total":        p.get("total", 0),
            "known":        p.get("known", 0),
            "last_studied": p.get("last_studied"),
        },
        "is_group":    False,
    }

SEMESTER_RE = re.compile(r'(?:SoSe|WiSe|SS|WS)\s*\d{2}', re.IGNORECASE)

def get_courses():
    progress = load_progress()
    dirs = [d for d in COURSES_DIR.iterdir() if d.is_dir()]

    def sort_key(d):
        is_sem = bool(SEMESTER_RE.search(d.name))
        # Semester dirs first (newest first via reverse name sort), then others alphabetically
        return (0 if is_sem else 1, d.name if not is_sem else "".join(reversed(d.name)))

    semester_items = []
    other_items = []

    for d in sorted(dirs, key=lambda d: d.name):
        is_semester = bool(SEMESTER_RE.search(d.name))
        sub_dirs = sorted([sd for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith('.')])
        if is_semester or len(sub_dirs) >= 2:
            sub_courses = [
                _course_info(f"{d.name}/{sd.name}", sd, progress)
                for sd in sub_dirs
            ]
            entry = {
                "name":        d.name,
                "path":        d.name,
                "is_group":    True,
                "is_semester": is_semester,
                "courses":     sub_courses,
            }
            if is_semester:
                semester_items.append(entry)
            else:
                other_items.append(entry)
        else:
            other_items.append(_course_info(d.name, d, progress))

    # Semester groups newest-first (reverse alphabetical on name works for "SoSe/WiSe YYYY")
    semester_items.sort(key=lambda x: x["name"], reverse=True)
    return semester_items + other_items

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
        elif suffix == ".pptx":
            from pptx import Presentation
            prs = Presentation(str(path))
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                texts = [shape.text.strip() for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
                if texts:
                    parts.append(f"[Slide {i}]\n" + "\n".join(texts))
            return "\n\n".join(parts)
    except Exception as e:
        return f"Fehler beim Lesen: {e}"
    return ""

def parse_flashcards(summary_md: str) -> list[dict]:
    """Extract Q&A pairs from summary markdown."""
    cards = []
    sections = re.split(r'\n## ', summary_md)
    for section in sections:
        lines = section.strip().split('\n')
        section_title = lines[0].strip('# ').strip() if lines else "Unbekannt"

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

@app.route("/api/summary-raw/<path:course_name>")
def api_summary_raw(course_name):
    path = COURSES_DIR / course_name / OUTPUT_FILENAME
    if not path.exists():
        return "Not found", 404
    return send_file(str(path), as_attachment=True, download_name=f"{course_name}_zusammenfassung.md")

@app.route("/api/flashcards/<path:course_name>")
def api_flashcards(course_name):
    path = COURSES_DIR / course_name / OUTPUT_FILENAME
    if not path.exists():
        return jsonify([])
    md    = path.read_text(encoding="utf-8")
    cards = parse_flashcards(md)
    return jsonify(cards)

def _collect_flashcards(rel_path: str, d: Path) -> list:
    summary = d / OUTPUT_FILENAME
    if not summary.exists():
        return []
    md = summary.read_text(encoding="utf-8")
    cards = parse_flashcards(md)
    for c in cards:
        c["course"] = d.name
        c["id"] = f"{rel_path}::{c['id']}"
    return cards

@app.route("/api/all-flashcards")
def api_all_flashcards():
    """Return all flashcards from all courses combined."""
    all_cards = []
    for d in sorted(COURSES_DIR.iterdir()):
        if not d.is_dir():
            continue
        sub_dirs = [sd for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith('.')]
        if len(sub_dirs) >= 2:
            for sd in sorted(sub_dirs):
                all_cards.extend(_collect_flashcards(f"{d.name}/{sd.name}", sd))
        else:
            all_cards.extend(_collect_flashcards(d.name, d))
    return jsonify(all_cards)

@app.route("/api/notes/<path:course_name>", methods=["GET", "POST"])
def api_notes(course_name):
    path = COURSES_DIR / course_name / NOTES_FILENAME
    if request.method == "POST":
        text = request.json.get("text", "")
        path.write_text(text, encoding="utf-8")
        return jsonify({"ok": True})
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return jsonify({"text": text})

def _update_streak(prog: dict) -> dict:
    """Update the global learning streak stored under _streak key."""
    today = datetime.now().date().isoformat()
    from datetime import timedelta
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    s = prog.get("_streak", {"count": 0, "last_date": None})
    if s["last_date"] == today:
        pass  # already counted today
    elif s["last_date"] == yesterday:
        s["count"] += 1
    else:
        s["count"] = 1
    s["last_date"] = today
    prog["_streak"] = s
    return prog

@app.route("/api/progress/<path:course_name>", methods=["GET", "POST"])
def api_progress(course_name):
    prog = load_progress()
    if request.method == "POST":
        data = request.json
        data["last_studied"] = datetime.now().isoformat(timespec="seconds")
        prog[course_name] = data
        prog = _update_streak(prog)
        save_progress(prog)
        return jsonify({"ok": True})
    return jsonify(prog.get(course_name, {"total": 0, "known": 0, "cards": {}}))

@app.route("/api/streak")
def api_streak():
    prog = load_progress()
    return jsonify(prog.get("_streak", {"count": 0, "last_date": None}))

@app.route("/api/progress-reset/<path:course_name>", methods=["POST"])
def api_progress_reset(course_name):
    prog = load_progress()
    if course_name in prog:
        del prog[course_name]
        save_progress(prog)
    return jsonify({"ok": True})

@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    data   = request.json
    course = data.get("course", "")
    limit  = data.get("limit", 3)
    force  = data.get("force", False)
    files  = data.get("files", [])
    lang   = data.get("lang", "en")

    # Resolve to absolute path so nested courses (e.g. "Archiv/Machine Learning") work
    course_dir = COURSES_DIR / course
    cmd = [PYTHON, SUMMARIZE_SCRIPT, "--dir", str(course_dir), "--limit", str(limit), "--lang", lang]
    if force:
        cmd.append("--force")
    if files:
        cmd += ["--files"] + files

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return jsonify({"success": result.returncode == 0, "log": result.stdout + result.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "log": "Timeout after 5 minutes."})
    except Exception as e:
        return jsonify({"success": False, "log": str(e)})

@app.route("/api/summarize-all", methods=["POST"])
def api_summarize_all():
    """Summarise all courses that don't have a summary yet."""
    lang  = request.json.get("lang", "en")
    limit = request.json.get("limit", 3)
    log_lines = []
    errors = 0
    courses = get_courses()
    pending = []
    for item in courses:
        if item.get("is_group"):
            for sub in item.get("courses", []):
                if not sub["has_summary"] and sub["file_count"] > 0:
                    pending.append(sub["path"])
        else:
            if not item["has_summary"] and item["file_count"] > 0:
                pending.append(item["path"])

    for path in pending:
        course_dir = COURSES_DIR / path
        cmd = [PYTHON, SUMMARIZE_SCRIPT, "--dir", str(course_dir), "--limit", str(limit), "--lang", lang]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            log_lines.append(f"✓ {path}" if r.returncode == 0 else f"✗ {path}: {r.stderr[:120]}")
            if r.returncode != 0:
                errors += 1
        except Exception as e:
            log_lines.append(f"✗ {path}: {e}")
            errors += 1

    return jsonify({"success": errors == 0, "done": len(pending) - errors, "count": len(pending), "log": "\n".join(log_lines) or "Nichts zu tun."})

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

def _snippets(text: str, q: str, max_matches: int = 3) -> list[str]:
    matches, start, lower = [], 0, text.lower()
    while True:
        idx = lower.find(q, start)
        if idx == -1 or len(matches) >= max_matches:
            break
        snippet = text[max(0, idx-80):idx+120].replace('\n', ' ')
        matches.append(re.sub(r'\s+', ' ', snippet).strip())
        start = idx + 1
    return matches

def _search_dir(q: str, rel_path: str, d: Path, results: list):
    """Search summary and notes in a single course directory."""
    hits = []
    source = None
    # Search summary first
    summary = d / OUTPUT_FILENAME
    if summary.exists():
        md = summary.read_text(encoding="utf-8", errors="replace")
        snips = _snippets(md, q)
        if snips:
            hits = snips
            source = "summary"
    # Also search notes
    notes = d / NOTES_FILENAME
    if notes.exists():
        nd = notes.read_text(encoding="utf-8", errors="replace")
        nsnips = _snippets(nd, q)
        if nsnips and not hits:
            hits = nsnips
            source = "notes"
        elif nsnips:
            source = "both"
    if hits:
        results.append({
            "course":  rel_path,
            "name":    d.name,
            "snippet": hits[0],
            "count":   len(hits),
            "source":  source,
        })

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify([])
    results = []
    for d in sorted(COURSES_DIR.iterdir()):
        if not d.is_dir():
            continue
        sub_dirs = [sd for sd in d.iterdir() if sd.is_dir() and not sd.name.startswith('.')]
        if len(sub_dirs) >= 2:
            for sd in sorted(sub_dirs):
                _search_dir(q, f"{d.name}/{sd.name}", sd, results)
        else:
            _search_dir(q, d.name, d, results)
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
  --bg:        #0c0e17;
  --bg2:       #111320;
  --bg3:       #181b2e;
  --bg4:       #1e2138;
  --bg5:       #242849;
  --border:    #232640;
  --border2:   #2d3155;
  --text:      #e4eaf4;
  --text2:     #8fa3bf;
  --text3:     #49597a;
  --blue:      #4f8ef7;
  --blue2:     #2563eb;
  --blue3:     #1e40af;
  --green:     #34d399;
  --yellow:    #fbbf24;
  --red:       #f87171;
  --purple:    #a78bfa;
  --orange:    #fb923c;
  --radius:    9px;
  --radius-lg: 14px;
  --radius-xl: 18px;
  --shadow-sm: 0 1px 3px rgba(0,0,0,.4);
  --shadow:    0 4px 16px rgba(0,0,0,.5);
  --shadow-lg: 0 8px 32px rgba(0,0,0,.6);
  --transition: 160ms ease;
  --glow-blue: 0 0 20px rgba(79,142,247,.15);
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}

/* ── Topbar ── */
#topbar {
  height: 54px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 12px;
  flex-shrink: 0;
  z-index: 10;
  box-shadow: 0 1px 0 var(--border), 0 2px 12px rgba(0,0,0,.3);
}
#topbar-logo {
  font-size: 15px; font-weight: 800; color: #fff;
  display: flex; align-items: center; gap: 8px; letter-spacing: -.01em;
  text-shadow: 0 0 20px rgba(79,142,247,.4);
}
#topbar-logo .logo-icon { font-size: 18px; }
#topbar-logo span { color: var(--blue); }

#search-global {
  flex: 1; max-width: 340px;
  padding: 8px 14px 8px 36px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 22px;
  color: var(--text);
  font-size: 13px;
  outline: none;
  transition: border-color var(--transition), box-shadow var(--transition);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2349597a' stroke-width='2.5'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.35-4.35'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: 12px center;
}
#search-global:focus { border-color: var(--blue); box-shadow: var(--glow-blue); }
#search-global::placeholder { color: var(--text3); }

.tbtn {
  padding: 7px 15px;
  border-radius: var(--radius);
  border: none;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  transition: transform var(--transition), box-shadow var(--transition), opacity var(--transition), background var(--transition);
  white-space: nowrap;
  letter-spacing: -.01em;
}
.tbtn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: var(--shadow); opacity: .93; }
.tbtn:active:not(:disabled) { transform: translateY(0); box-shadow: none; }
.tbtn:disabled { opacity: .32; cursor: not-allowed; }
.btn-blue   { background: linear-gradient(135deg, #5597ff, var(--blue2)); color: #fff; box-shadow: 0 2px 8px rgba(79,142,247,.3); }
.btn-blue:hover:not(:disabled) { box-shadow: 0 4px 16px rgba(79,142,247,.45); }
.btn-gray   { background: var(--bg4); color: var(--text2); border: 1px solid var(--border2); }
.btn-green  { background: linear-gradient(135deg, #059669, #047857); color: #a7f3d0; box-shadow: 0 2px 8px rgba(52,211,153,.2); }
.btn-red    { background: linear-gradient(135deg, #dc2626, #b91c1c); color: #fecaca; }
.btn-purple { background: linear-gradient(135deg, #7c3aed, #5b21b6); color: #ddd6fe; box-shadow: 0 2px 8px rgba(167,139,250,.25); }

/* ── Layout ── */
#layout { display: flex; flex: 1; overflow: hidden; }

/* ── Sidebar ── */
#sidebar {
  width: 260px; min-width: 150px; max-width: 480px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
}

#sidebar-top {
  padding: 12px 10px 8px;
  border-bottom: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 8px;
  background: var(--bg2);
}
#sidebar-search {
  width: 100%; padding: 8px 11px;
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: var(--radius); color: var(--text); font-size: 12px; outline: none;
  transition: border-color var(--transition);
}
#sidebar-search:focus { border-color: var(--blue); }
#sidebar-search::placeholder { color: var(--text3); }

/* Filter pills */
.filter-pills { display: flex; gap: 4px; flex-wrap: wrap; }
.pill {
  padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 500;
  cursor: pointer; border: 1px solid var(--border); background: none;
  color: var(--text3); transition: all var(--transition);
}
.pill:hover { border-color: var(--border2); color: var(--text2); background: var(--bg3); }
.pill.active { background: var(--blue); border-color: var(--blue); color: #fff; box-shadow: 0 2px 8px rgba(79,142,247,.3); }

/* Sort row */
.sort-row { display: flex; align-items: center; gap: 6px; }
.sort-row label { font-size: 10px; color: var(--text3); flex-shrink: 0; }
.sort-select {
  flex: 1; font-size: 11px; background: var(--bg3); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text2); padding: 4px 6px; outline: none; cursor: pointer;
}

.sidebar-section-label {
  padding: 10px 10px 3px;
  font-size: 9px; font-weight: 700;
  color: var(--text3); text-transform: uppercase; letter-spacing: .1em;
}

#course-list { overflow-y: auto; flex: 1; padding: 4px 6px 4px; }

.citem {
  padding: 9px 10px; border-radius: var(--radius);
  cursor: pointer; display: flex; align-items: center; gap: 9px;
  margin-bottom: 1px;
  transition: background var(--transition), border-color var(--transition);
  position: relative; border-left: 2px solid transparent;
}
.citem:hover  { background: var(--bg3); }
.citem.active { background: rgba(79,142,247,.1); border-left-color: var(--blue); }

.citem-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.dot-ok     { background: var(--green); box-shadow: 0 0 5px rgba(52,211,153,.5); }
.dot-missing{ background: var(--text3); }

.citem-body { flex: 1; min-width: 0; }
.citem-name { font-size: 12px; font-weight: 500; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: color var(--transition); }
.citem.active .citem-name { color: var(--text); }
.citem-meta { font-size: 10px; color: var(--text3); display: flex; gap: 6px; margin-top: 2px; flex-wrap: wrap; }

.progress-mini { height: 2px; background: var(--border); border-radius: 2px; margin-top: 5px; }
.progress-mini-fill {
  height: 100%; border-radius: 2px; transition: width .4s ease;
  background: linear-gradient(90deg, var(--blue), var(--purple));
}

/* Favorite star button */
.fav-btn {
  background: none; border: none; cursor: pointer;
  font-size: 12px; padding: 3px 5px; flex-shrink: 0;
  opacity: 0.25; transition: opacity var(--transition), transform var(--transition);
  line-height: 1;
}
.fav-btn:hover { opacity: .8; transform: scale(1.25); }
.fav-btn.is-fav { opacity: 1; }

/* Group headers in sidebar */
.group-header {
  display: flex; align-items: center; gap: 7px; padding: 7px 10px;
  cursor: pointer; border-radius: var(--radius);
  transition: background var(--transition);
  margin: 4px 0 1px; user-select: none; border-left: 2px solid transparent;
}
.group-header:hover { background: var(--bg3); }
.group-chevron { font-size: 8px; color: var(--text3); width: 10px; flex-shrink: 0; transition: transform var(--transition); }
.group-name { font-size: 10px; font-weight: 700; color: var(--text3); text-transform: uppercase; letter-spacing: .08em; flex: 1; }
.group-meta { font-size: 10px; color: var(--text3); background: var(--bg3); padding: 1px 6px; border-radius: 8px; }

/* Semester group header — visually distinct */
.semester-header { border-left-color: var(--blue); margin-top: 10px; }
.semester-header .group-name { color: var(--blue); letter-spacing: .06em; }
.semester-header .group-meta { background: rgba(79,142,247,.12); color: var(--blue); }

/* Global learn button at sidebar bottom */
#global-learn-btn {
  margin: 8px 8px 10px; flex-shrink: 0;
  padding: 9px 12px; font-size: 12px; font-weight: 700;
  background: linear-gradient(135deg, rgba(124,58,237,.25), rgba(91,33,182,.2));
  border: 1px solid rgba(167,139,250,.3); color: #c4b5fd;
  border-radius: var(--radius); cursor: pointer; text-align: center;
  transition: all var(--transition);
  letter-spacing: .01em;
}
#global-learn-btn:hover {
  background: linear-gradient(135deg, rgba(124,58,237,.4), rgba(91,33,182,.35));
  border-color: rgba(167,139,250,.5); transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(124,58,237,.2);
}

/* ── Resize divider ── */
.resize-divider {
  width: 5px; background: transparent; cursor: col-resize;
  flex-shrink: 0; position: relative; z-index: 5; transition: background var(--transition);
}
.resize-divider:hover, .resize-divider.dragging { background: var(--blue); opacity: .6; }

/* ── Main ── */
#main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }

/* ── Tabs ── */
#tabs {
  display: flex; align-items: stretch;
  padding: 0 16px; gap: 2px;
  background: var(--bg2); border-bottom: 1px solid var(--border);
  height: 44px; flex-shrink: 0;
}
.tab {
  padding: 0 14px; border-radius: 0; cursor: pointer;
  font-size: 13px; font-weight: 500; color: var(--text3);
  transition: color var(--transition), background var(--transition); border: none; background: none;
  display: flex; align-items: center; gap: 5px;
  position: relative;
}
.tab:hover { color: var(--text2); }
.tab.active { color: var(--text); }
.tab.active::after {
  content: ''; position: absolute; bottom: 0; left: 8px; right: 8px;
  height: 2px; background: var(--blue); border-radius: 2px 2px 0 0;
}
.tab-spacer { flex: 1; }
#tabs-course-label {
  font-size: 11px; color: var(--text3); font-weight: 400;
  max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 0 4px;
}

/* ── Content ── */
#content { flex: 1; overflow: hidden; position: relative; }

/* IMPORTANT: panels use display:none by default — never add display:flex/block to panel IDs */
.panel { position: absolute; inset: 0; overflow-y: auto; display: none; padding: 30px 38px; }
.panel.active { display: block; animation: panelIn .18s ease; }
@keyframes panelIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
/* Special panels that need flex layout: use inner wrapper with class .panel-inner-flex */
.panel-inner-flex { display: flex; flex-direction: column; height: 100%; }

/* Home panel */
.stats-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 28px; }
.stat-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 18px 20px; position: relative; overflow: hidden;
  transition: border-color var(--transition), transform var(--transition), box-shadow var(--transition);
}
.stat-card:hover { border-color: var(--border2); transform: translateY(-2px); box-shadow: var(--shadow); }
.stat-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: var(--accent, var(--blue));
}
.stat-label { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 10px; font-weight: 600; }
.stat-value { font-size: 32px; font-weight: 800; color: var(--text); letter-spacing: -.02em; line-height: 1; }
.stat-sub   { font-size: 11px; color: var(--text3); margin-top: 6px; }
.stat-bar { height: 3px; background: var(--border); border-radius: 3px; margin-top: 10px; overflow: hidden; }
.stat-bar-fill { height: 100%; border-radius: 3px; transition: width .6s cubic-bezier(.4,0,.2,1); }

.section-title {
  font-size: 10px; font-weight: 700; color: var(--text3); margin-bottom: 12px;
  text-transform: uppercase; letter-spacing: .1em;
}

.pipeline-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 14px 20px; display: flex; align-items: center; gap: 14px; margin-bottom: 24px;
}
.pipeline-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }

.recent-list { display: flex; flex-direction: column; gap: 6px; }
.recent-item {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 11px 16px; cursor: pointer; display: flex; align-items: center; gap: 12px;
  transition: border-color var(--transition), transform var(--transition), box-shadow var(--transition);
}
.recent-item:hover { border-color: var(--blue); transform: translateX(3px); box-shadow: var(--shadow-sm); }
.recent-item-name { flex: 1; font-size: 13px; color: var(--text); font-weight: 500; }
.recent-item-meta { font-size: 11px; color: var(--text3); }
.recent-item-progress { min-width: 80px; }
.recent-progress-bar { height: 3px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 4px; }
.recent-progress-fill { height: 100%; background: linear-gradient(90deg, var(--blue), var(--purple)); border-radius: 3px; }

/* Files panel */
.files-layout { display: flex; height: 100%; overflow: hidden; gap: 0; }
.files-list-col { width: 260px; min-width: 140px; max-width: 500px; flex-shrink: 0; overflow-y: auto; padding: 0 2px 0 0; }
.files-preview-col { flex: 1; min-width: 0; overflow: hidden; }

.file-item {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: var(--radius); cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
  border: 1px solid transparent; margin-bottom: 1px;
}
.file-item:hover { background: var(--bg3); }
.file-item.active { background: var(--bg4); border-color: rgba(79,142,247,.4); }
.file-item.new-file { border-color: rgba(251,191,36,.25); }
.file-item input[type=checkbox] { accent-color: var(--blue); flex-shrink: 0; cursor: pointer; }
.file-icon { font-size: 15px; flex-shrink: 0; }
.file-name { font-size: 12px; color: var(--text2); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.file-actions {
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 7px;
}
.limit-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text3); }
.limit-input {
  width: 55px; padding: 5px 8px; background: var(--bg3);
  border: 1px solid var(--border); border-radius: var(--radius);
  color: var(--text); font-size: 12px; text-align: center; outline: none;
  transition: border-color var(--transition);
}
.limit-input:focus { border-color: var(--blue); }

.preview-box {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius-lg);
  height: 100%; overflow: hidden; display: flex; flex-direction: column;
  box-shadow: var(--shadow-sm);
}
.preview-header {
  padding: 10px 16px; border-bottom: 1px solid var(--border);
  font-size: 12px; color: var(--text3); background: var(--bg3);
  border-radius: var(--radius-lg) var(--radius-lg) 0 0; flex-shrink: 0;
  display: flex; align-items: center; gap: 8px;
}
.preview-header-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; color: var(--text2); }
.preview-body {
  flex: 1; overflow-y: auto; padding: 20px;
  font-family: "SF Mono", "JetBrains Mono", monospace; font-size: 12px;
  color: var(--text2); line-height: 1.75; white-space: pre-wrap; word-break: break-word;
}
.preview-body.pdf-wrap { padding: 0; }
.preview-body iframe { width: 100%; height: 100%; border: none; }
.preview-placeholder {
  height: 100%; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 10px; color: var(--text3);
}
.preview-placeholder .icon { font-size: 40px; opacity: .5; }

/* Summary panel */
.summary-toolbar {
  display: flex; align-items: center; gap: 8px; margin-bottom: 24px;
  padding-bottom: 18px; border-bottom: 1px solid var(--border); flex-wrap: wrap;
}
.md-content { max-width: 820px; }
.md-content h1 { font-size: 24px; color: var(--text); margin-bottom: 8px; line-height: 1.25; font-weight: 800; letter-spacing: -.02em; }
.md-content h2 { font-size: 17px; color: #93c5fd; margin: 32px 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); font-weight: 700; }
.md-content h3 { font-size: 14px; color: #a5b4fc; margin: 20px 0 8px; font-weight: 600; }
.md-content p  { color: var(--text2); line-height: 1.8; margin-bottom: 12px; }
.md-content ul, .md-content ol { color: var(--text2); line-height: 1.8; margin: 6px 0 14px 24px; }
.md-content li { margin-bottom: 5px; }
.md-content strong { color: var(--text); }
.md-content em { color: var(--text3); }
.md-content hr { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
.md-content code { background: var(--bg3); padding: 2px 7px; border-radius: 5px; font-size: 12px; color: #6ee7b7; font-family: "SF Mono", monospace; border: 1px solid var(--border); }
.md-content blockquote { border-left: 3px solid var(--blue); padding: 8px 16px; color: var(--text3); margin: 14px 0; background: rgba(79,142,247,.05); border-radius: 0 var(--radius) var(--radius) 0; }
.md-content table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13px; border-radius: var(--radius); overflow: hidden; }
.md-content th { background: var(--bg3); padding: 9px 14px; text-align: left; color: #93c5fd; border: 1px solid var(--border); font-weight: 600; }
.md-content td { padding: 8px 14px; border: 1px solid var(--border); color: var(--text2); }
.md-content tr:nth-child(even) td { background: rgba(255,255,255,.02); }

/* Flashcard panel */
.flash-layout { max-width: 700px; margin: 0 auto; }
.flash-header { display: flex; align-items: center; gap: 10px; margin-bottom: 22px; flex-wrap: wrap; }
.flash-header-title { font-size: 14px; font-weight: 600; color: var(--text2); flex: 1; }
.flash-progress-bar { background: var(--border); border-radius: 10px; height: 5px; margin-bottom: 14px; overflow: hidden; }
.flash-progress-fill {
  height: 100%; border-radius: 10px; transition: width .5s cubic-bezier(.4,0,.2,1);
  background: linear-gradient(90deg, var(--blue), var(--purple));
}
.flash-meta {
  font-size: 12px; color: var(--text3); text-align: center; margin-bottom: 24px;
  display: flex; justify-content: center; align-items: center; gap: 16px;
}
.flash-meta span { display: flex; align-items: center; gap: 4px; }
.flash-timer { font-size: 12px; color: var(--text3); font-variant-numeric: tabular-nums; letter-spacing: .03em; }

.flash-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius-xl); padding: 40px 36px; text-align: center;
  min-height: 240px; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 16px;
  margin-bottom: 24px;
  transition: border-color .25s, box-shadow .25s, transform .15s;
  box-shadow: var(--shadow-sm);
}
.flash-card:hover { box-shadow: var(--shadow); }
.flash-card.flash-card-known   { border-color: var(--green); box-shadow: 0 0 0 1px var(--green), 0 4px 20px rgba(52,211,153,.15); }
.flash-card.flash-card-unknown { border-color: var(--red);   box-shadow: 0 0 0 1px var(--red),   0 4px 20px rgba(248,113,113,.15); }
.flash-section { font-size: 10px; color: var(--text3); text-transform: uppercase; letter-spacing: .08em; font-weight: 600; }
.flash-course-badge { font-size: 10px; color: var(--purple); background: rgba(167,139,250,.12); padding: 3px 10px; border-radius: 12px; border: 1px solid rgba(167,139,250,.25); }
.flash-question { font-size: 19px; color: var(--text); line-height: 1.5; font-weight: 600; letter-spacing: -.01em; }
.flash-answer {
  background: var(--bg3); border: 1px solid var(--border2);
  border-radius: var(--radius); padding: 16px 22px;
  font-size: 14px; color: var(--text2); line-height: 1.7;
  display: none; text-align: left; width: 100%;
  animation: fadeIn .2s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

.flash-btns { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
.flash-btn {
  padding: 12px 28px; border-radius: var(--radius); border: none;
  cursor: pointer; font-size: 14px; font-weight: 700;
  transition: transform var(--transition), box-shadow var(--transition), opacity var(--transition);
  letter-spacing: -.01em;
}
.flash-btn:hover { transform: translateY(-2px); opacity: .92; }
.flash-btn:active { transform: translateY(0); }
.fb-reveal  { background: var(--bg4); color: var(--text); border: 1px solid var(--border2); padding: 12px 36px; }
.fb-known   { background: linear-gradient(135deg, #059669, #047857); color: #a7f3d0; box-shadow: 0 2px 12px rgba(52,211,153,.3); }
.fb-unknown { background: linear-gradient(135deg, #dc2626, #b91c1c); color: #fecaca; box-shadow: 0 2px 12px rgba(248,113,113,.3); }
.fb-known:hover   { box-shadow: 0 4px 20px rgba(52,211,153,.45); }
.fb-unknown:hover { box-shadow: 0 4px 20px rgba(248,113,113,.45); }

.flash-done {
  text-align: center; padding: 48px 40px;
  display: none; flex-direction: column; align-items: center; gap: 14px;
}
.flash-done .big-icon { font-size: 64px; }
.flash-done h2 { font-size: 24px; color: var(--text); font-weight: 800; }
.flash-done p  { color: var(--text3); font-size: 14px; line-height: 1.6; }

.flash-kbd-hint { text-align: center; font-size: 11px; color: var(--text3); margin-top: 10px; }
.flash-kbd-hint kbd {
  background: var(--bg3); border: 1px solid var(--border2); border-radius: 5px;
  padding: 2px 6px; font-size: 10px; font-family: "SF Mono", monospace; color: var(--text2);
}

/* Notes panel */
.notes-panel { display: flex; flex-direction: column; height: calc(100vh - 158px); }
.notes-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
#notes-editor {
  flex: 1; background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius-lg); padding: 20px 22px; color: var(--text2);
  font-size: 14px; line-height: 1.8; resize: none; outline: none;
  font-family: inherit; transition: border-color var(--transition), box-shadow var(--transition);
}
#notes-editor:focus { border-color: var(--blue); box-shadow: var(--glow-blue); }
#notes-preview {
  flex: 1; background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius-lg); padding: 20px 22px; overflow-y: auto;
  display: none;
}
#notes-saved { font-size: 12px; display: none; }

/* Search results */
.search-results-header { font-size: 12px; color: var(--text3); margin-bottom: 16px; }
.search-result {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 18px; margin-bottom: 8px;
  cursor: pointer; transition: border-color var(--transition), transform var(--transition);
}
.search-result:hover { border-color: var(--blue); transform: translateX(3px); }
.search-result-course { font-size: 12px; color: var(--blue); margin-bottom: 5px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.search-result-count { font-size: 10px; background: rgba(79,142,247,.15); color: var(--blue); padding: 1px 6px; border-radius: 10px; }
.search-result-snippet { font-size: 13px; color: var(--text2); line-height: 1.6; }
.search-result-snippet mark { background: rgba(79,142,247,.2); color: #93c5fd; border-radius: 3px; padding: 0 3px; }
.search-empty { text-align: center; padding: 60px; color: var(--text3); font-size: 14px; }

/* Log */
#log-box {
  position: fixed; bottom: 16px; right: 16px; width: 440px;
  background: rgba(10,12,22,.97); border: 1px solid var(--border); border-radius: var(--radius-lg);
  font-family: "SF Mono", monospace; font-size: 11px;
  color: #4ade80; display: none; z-index: 100; box-shadow: var(--shadow-lg);
  flex-direction: column; max-height: 280px; backdrop-filter: blur(12px);
}
#log-header { padding: 9px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }
#log-title { flex: 1; font-size: 11px; color: var(--text3); font-family: sans-serif; }
#log-close { cursor: pointer; color: var(--text3); font-size: 14px; background: none; border: none; transition: color var(--transition); }
#log-close:hover { color: var(--text); }
#log-content { padding: 12px 14px; white-space: pre-wrap; overflow-y: auto; flex: 1; line-height: 1.6; }

/* Toast */
#toast-container {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: 6px; z-index: 200;
  pointer-events: none; align-items: center;
}
.toast {
  background: rgba(24,27,46,.95); border: 1px solid var(--border2); border-radius: 20px;
  padding: 9px 18px; font-size: 13px; color: var(--text2);
  box-shadow: var(--shadow-lg); backdrop-filter: blur(12px);
  animation: toastIn .2s cubic-bezier(.4,0,.2,1);
  display: flex; align-items: center; gap: 8px; white-space: nowrap;
}
.toast.toast-ok  { border-color: rgba(52,211,153,.4); color: #6ee7b7; }
.toast.toast-err { border-color: rgba(248,113,113,.4); color: #fca5a5; }
@keyframes toastIn { from { opacity: 0; transform: translateY(8px) scale(.95); } to { opacity: 1; transform: none; } }

/* Keyboard shortcuts overlay */
#shortcuts-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 300;
  display: none; align-items: center; justify-content: center;
  backdrop-filter: blur(4px);
}
#shortcuts-overlay.open { display: flex; }
.shortcuts-box {
  background: var(--bg2); border: 1px solid var(--border2); border-radius: var(--radius-xl);
  padding: 30px 34px; max-width: 500px; width: 90%;
  box-shadow: var(--shadow-lg);
  animation: panelIn .2s ease;
}
.shortcuts-box h3 { font-size: 17px; color: var(--text); margin-bottom: 20px; font-weight: 700; }
.shortcut-row { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; }
.shortcut-row kbd {
  background: var(--bg4); border: 1px solid var(--border2); border-radius: 6px;
  padding: 3px 9px; font-size: 11px; font-family: "SF Mono", monospace;
  color: var(--text2); min-width: 40px; text-align: center; flex-shrink: 0;
  box-shadow: 0 2px 0 var(--border);
}
.shortcut-desc { font-size: 13px; color: var(--text3); }
.shortcuts-section { font-size: 9px; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); margin: 16px 0 8px; font-weight: 700; }

/* Confirm modal */
#confirm-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 300;
  display: none; align-items: center; justify-content: center; backdrop-filter: blur(4px);
}
#confirm-overlay.open { display: flex; }
.confirm-box {
  background: var(--bg2); border: 1px solid var(--border2); border-radius: var(--radius-xl);
  padding: 30px 34px; max-width: 380px; width: 90%; text-align: center;
  box-shadow: var(--shadow-lg); animation: panelIn .2s ease;
}
.confirm-box h3 { font-size: 17px; color: var(--text); margin-bottom: 10px; font-weight: 700; }
.confirm-box p { font-size: 13px; color: var(--text3); margin-bottom: 24px; line-height: 1.6; }
.confirm-btns { display: flex; gap: 10px; justify-content: center; }

/* Spinner */
.spin {
  display: inline-block; width: 13px; height: 13px;
  border: 2px solid var(--border2); border-top-color: var(--blue);
  border-radius: 50%; animation: spin .6s linear infinite; vertical-align: middle;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Empty state */
.empty-state {
  height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  color: var(--text3); gap: 14px; text-align: center; padding: 40px;
}
.empty-state .icon { font-size: 56px; opacity: .6; }
.empty-state h3 { font-size: 17px; color: var(--text2); font-weight: 600; }
.empty-state p { font-size: 13px; max-width: 300px; line-height: 1.7; }

/* Scrollbar */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: var(--text3); }

/* files panel */
#panel-files { padding: 16px 20px; }

/* New-files badge */
.new-badge {
  display: inline-block; font-size: 9px; font-weight: 700;
  background: var(--yellow); color: #000; border-radius: 8px;
  padding: 1px 5px; vertical-align: middle; margin-left: 5px; letter-spacing: .02em;
}

/* File meta */
.file-meta-info { font-size: 10px; color: var(--text3); margin-left: auto; flex-shrink: 0; padding-left: 6px; }

/* Chat panel — NO display override here, uses .panel base class */
#panel-chat { padding: 0; overflow: hidden; }
.chat-layout { display: flex; flex-direction: column; height: 100%; }
.chat-messages {
  flex: 1; overflow-y: auto; padding: 28px 40px; display: flex; flex-direction: column; gap: 18px;
}
.chat-msg { display: flex; gap: 12px; max-width: 760px; animation: fadeIn .2s ease; }
.chat-msg.user { align-self: flex-end; flex-direction: row-reverse; }
.chat-avatar {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; font-size: 15px;
  background: var(--bg4); border: 1px solid var(--border2);
}
.chat-msg.user .chat-avatar { background: linear-gradient(135deg, var(--blue2), var(--blue3)); }
.chat-bubble {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 13px 18px; font-size: 13.5px; color: var(--text2); line-height: 1.7;
  max-width: 600px; box-shadow: var(--shadow-sm);
}
.chat-msg.user .chat-bubble {
  background: linear-gradient(135deg, rgba(37,99,235,.25), rgba(30,64,175,.2));
  border-color: rgba(79,142,247,.35); color: var(--text);
  border-radius: var(--radius-lg) var(--radius-lg) 4px var(--radius-lg);
}
.chat-msg:not(.user) .chat-bubble { border-radius: var(--radius-lg) var(--radius-lg) var(--radius-lg) 4px; }
.chat-bubble.streaming::after { content: '▋'; animation: blink .8s step-end infinite; color: var(--blue); }
@keyframes blink { 50% { opacity: 0; } }
.chat-bubble p { margin-bottom: 8px; }
.chat-bubble p:last-child { margin-bottom: 0; }
.chat-input-row {
  padding: 14px 24px; border-top: 1px solid var(--border); background: var(--bg2);
  display: flex; gap: 10px; align-items: flex-end; flex-shrink: 0;
}
.chat-input-row textarea {
  flex: 1; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-lg);
  color: var(--text); font-size: 13px; padding: 11px 16px; resize: none; outline: none;
  font-family: inherit; line-height: 1.6; max-height: 120px; min-height: 44px;
  transition: border-color var(--transition), box-shadow var(--transition);
}
.chat-input-row textarea:focus { border-color: var(--blue); box-shadow: var(--glow-blue); }
.chat-input-row textarea::placeholder { color: var(--text3); }
.chat-input-btn {
  padding: 11px 18px; background: linear-gradient(135deg, #5597ff, var(--blue2)); color: #fff; border: none;
  border-radius: var(--radius-lg); cursor: pointer; font-size: 13px; font-weight: 700;
  transition: transform var(--transition), box-shadow var(--transition), opacity var(--transition);
  flex-shrink: 0; box-shadow: 0 2px 8px rgba(79,142,247,.3);
}
.chat-input-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 16px rgba(79,142,247,.45); }
.chat-input-btn:disabled { opacity: .35; cursor: not-allowed; transform: none; }
.chat-suggestions { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 40px 14px; }
.chat-suggestion {
  font-size: 12px; padding: 5px 12px; background: var(--bg3); border: 1px solid var(--border);
  border-radius: 16px; cursor: pointer; color: var(--text3);
  transition: all var(--transition);
}
.chat-suggestion:hover { border-color: var(--blue); color: var(--text2); background: var(--bg4); transform: translateY(-1px); }

/* Recommendations */
.rec-list { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 28px; }
.rec-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 13px 16px; cursor: pointer;
  transition: border-color var(--transition), transform var(--transition), box-shadow var(--transition);
  flex: 1; min-width: 180px; display: flex; flex-direction: column; gap: 5px;
  border-left: 3px solid var(--accent, var(--border));
}
.rec-card:hover { border-color: var(--blue); transform: translateY(-2px); box-shadow: var(--shadow); }
.rec-card-reason { font-size: 9px; color: var(--text3); text-transform: uppercase; letter-spacing: .08em; font-weight: 700; }
.rec-card-name { font-size: 13px; color: var(--text); font-weight: 600; }
.rec-card-meta { font-size: 11px; color: var(--text3); }

/* Last-studied label in sidebar */
.citem-last { font-size: 9px; color: var(--text3); margin-top: 1px; }

/* Search source badge */
.search-source { font-size: 10px; padding: 2px 7px; border-radius: 8px; font-weight: 500; }
.search-source-summary { background: rgba(79,142,247,.12); color: var(--blue); }
.search-source-notes   { background: rgba(167,139,250,.12); color: var(--purple); }
.search-source-both    { background: rgba(52,211,153,.12);  color: var(--green); }
</style>
</head>
<body>

<!-- Topbar -->
<div id="topbar">
  <div id="topbar-logo"><span class="logo-icon">📚</span> <span>Stud.IP</span> Dashboard</div>
  <input id="search-global" type="text" placeholder="🔍  Suche in allen Zusammenfassungen… (Ctrl+K)" oninput="handleGlobalSearch(event)">
  <button class="tbtn btn-gray" onclick="goHome()" title="Übersicht (Esc)">Übersicht</button>
  <button class="tbtn btn-gray" onclick="showShortcuts()" title="Tastenkürzel anzeigen (?)">⌨️</button>
  <button class="tbtn btn-blue" id="scrape-btn" onclick="runScraper()">↓ Neue Dateien</button>
</div>

<!-- Layout -->
<div id="layout">

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-top">
      <input id="sidebar-search" type="text" placeholder="Kurs suchen…" oninput="filterAndRenderSidebar()">
      <div class="filter-pills">
        <button class="pill active" data-filter="all"       onclick="setFilter('all')">Alle</button>
        <button class="pill"        data-filter="summary"   onclick="setFilter('summary')">✓ Zusammenfassung</button>
        <button class="pill"        data-filter="nosummary" onclick="setFilter('nosummary')">✗ Offen</button>
        <button class="pill"        data-filter="fav"       onclick="setFilter('fav')">⭐ Favoriten</button>
      </div>
      <div class="sort-row">
        <label>Sortieren:</label>
        <select class="sort-select" onchange="setSortAndRender(this.value)">
          <option value="name">Name</option>
          <option value="files">Dateien</option>
          <option value="progress">Fortschritt</option>
          <option value="recent">Zuletzt aktualisiert</option>
        </select>
      </div>
    </div>
    <div id="course-list"></div>
    <button id="global-learn-btn" onclick="startGlobalLearn()" title="Alle Lernkarten aus allen Kursen">
      🧠 Alle Karten lernen
    </button>
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
      <button class="tab"        data-tab="chat"    onclick="switchTab('chat')">💬 Fragen</button>
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
        <div id="recommendations-wrap"></div>
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
              <button class="tbtn btn-blue"  style="width:100%" onclick="generateSummary(false)">Zusammenfassen</button>
              <button class="tbtn btn-gray"  style="width:100%" onclick="generateSummary(true)">↺ Neu generieren</button>
            </div>
          </div>
          <!-- Files / Preview resize divider -->
          <div class="resize-divider" id="divider-files" title="Ziehen zum Anpassen"></div>
          <div class="files-preview-col" id="files-preview-col">
            <div class="preview-box">
              <div class="preview-header" id="preview-header">
                <span class="preview-header-name">Datei auswählen zum Anzeigen</span>
              </div>
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
            <button class="tbtn btn-gray" id="notes-preview-btn" onclick="toggleNotesPreview()">Vorschau</button>
            <button class="tbtn btn-blue" onclick="saveNotes()">Speichern</button>
          </div>
          <textarea id="notes-editor" placeholder="Eigene Notizen, Fragen, Zusammenhänge… (Markdown wird unterstützt)"></textarea>
          <div id="notes-preview" class="md-content"></div>
        </div>
      </div>

      <!-- Chat panel -->
      <div class="panel" id="panel-chat">
        <div id="chat-body">
          <div class="empty-state">
            <div class="icon">💬</div>
            <h3>Fragen stellen</h3>
            <p>Lade einen Kurs und stelle Fragen zur Zusammenfassung</p>
          </div>
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
<div id="log-box" style="display:none;flex-direction:column">
  <div id="log-header">
    <span id="log-title">Log</span>
    <button id="log-close" onclick="document.getElementById('log-box').style.display='none'">✕</button>
  </div>
  <div id="log-content"></div>
</div>

<!-- Toast container -->
<div id="toast-container"></div>

<!-- Keyboard shortcuts overlay -->
<div id="shortcuts-overlay" onclick="hideShortcuts()">
  <div class="shortcuts-box" onclick="event.stopPropagation()">
    <h3>⌨️ Tastenkürzel</h3>
    <div class="shortcuts-section">Global</div>
    <div class="shortcut-row"><kbd>?</kbd>          <span class="shortcut-desc">Tastenkürzel anzeigen</span></div>
    <div class="shortcut-row"><kbd>Ctrl+K</kbd>     <span class="shortcut-desc">Globale Suche fokussieren</span></div>
    <div class="shortcut-row"><kbd>Esc</kbd>        <span class="shortcut-desc">Zur Übersicht / Overlay schließen</span></div>
    <div class="shortcuts-section">Lernkarten</div>
    <div class="shortcut-row"><kbd>Space</kbd>      <span class="shortcut-desc">Antwort anzeigen</span></div>
    <div class="shortcut-row"><kbd>→ / k</kbd>      <span class="shortcut-desc">Gewusst</span></div>
    <div class="shortcut-row"><kbd>← / u</kbd>      <span class="shortcut-desc">Nicht gewusst</span></div>
    <div class="shortcuts-section">Navigation</div>
    <div class="shortcut-row"><kbd>1</kbd>          <span class="shortcut-desc">Tab Dateien</span></div>
    <div class="shortcut-row"><kbd>2</kbd>          <span class="shortcut-desc">Tab Zusammenfassung</span></div>
    <div class="shortcut-row"><kbd>3</kbd>          <span class="shortcut-desc">Tab Lernen</span></div>
    <div class="shortcut-row"><kbd>4</kbd>          <span class="shortcut-desc">Tab Notizen</span></div>
    <div class="shortcut-row"><kbd>5</kbd>          <span class="shortcut-desc">Tab Fragen (AI Chat)</span></div>
    <div style="margin-top:20px;text-align:right">
      <button class="tbtn btn-gray" onclick="hideShortcuts()">Schließen</button>
    </div>
  </div>
</div>

<!-- Confirm modal -->
<div id="confirm-overlay" onclick="hideConfirm()">
  <div class="confirm-box" onclick="event.stopPropagation()">
    <h3 id="confirm-title">Sicher?</h3>
    <p id="confirm-message">Diese Aktion kann nicht rückgängig gemacht werden.</p>
    <div class="confirm-btns">
      <button class="tbtn btn-gray" onclick="hideConfirm()">Abbrechen</button>
      <button class="tbtn btn-red" id="confirm-ok">Bestätigen</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════════════
let allCourses     = [];
let activeCourse   = null;
let activeTab      = 'home';
let flashState     = { cards: [], index: 0, revealed: false, progress: {}, timerStart: null, timerInterval: null, isGlobal: false };
let notesSaveTimer = null;
let allFilesChecked = true;
let sidebarFilter  = 'all';
let sidebarSort    = 'name';
let notesPreviewMode = false;

// ═══════════════════════════════════════════════════════════════════════════
// Favorites
// ═══════════════════════════════════════════════════════════════════════════
function getFavorites() {
  try { return JSON.parse(localStorage.getItem('fav_courses') || '[]'); } catch { return []; }
}
function setFavorites(arr) { localStorage.setItem('fav_courses', JSON.stringify(arr)); }
function isFavorite(name) { return getFavorites().includes(name); }
function toggleFavorite(name) {
  let favs = getFavorites();
  favs = favs.includes(name) ? favs.filter(f => f !== name) : [...favs, name];
  setFavorites(favs);
}

// ═══════════════════════════════════════════════════════════════════════════
// Boot
// ═══════════════════════════════════════════════════════════════════════════
// courseTree = raw API response (may include groups)
// allCourses = flat list of all individual courses (for stats/home)
let courseTree = [];

function flattenTree(tree) {
  const flat = [];
  for (const item of tree) {
    if (item.is_group) flat.push(...item.courses);
    else flat.push(item);
  }
  return flat;
}

async function boot() {
  const [tree, pipeline, streak] = await Promise.all([
    fetch('/api/courses').then(r => r.json()),
    fetch('/api/pipeline-status').then(r => r.json()),
    fetch('/api/streak').then(r => r.json()),
  ]);
  courseTree = tree;
  allCourses = flattenTree(tree);
  filterAndRenderSidebar();
  renderHome(allCourses, pipeline, streak);
  initResizeDividers();
  applyFontSize();
}

// ═══════════════════════════════════════════════════════════════════════════
// Sidebar
// ═══════════════════════════════════════════════════════════════════════════
function setFilter(f) {
  sidebarFilter = f;
  document.querySelectorAll('.pill').forEach(p => p.classList.toggle('active', p.dataset.filter === f));
  filterAndRenderSidebar();
}

function setSortAndRender(s) {
  sidebarSort = s;
  filterAndRenderSidebar();
}

function filterCourses(courses) {
  const q = document.getElementById('sidebar-search').value.toLowerCase();
  let result = courses.filter(c => c.name.toLowerCase().includes(q) || (c.path||'').toLowerCase().includes(q));
  if (sidebarFilter === 'summary')   result = result.filter(c => c.has_summary);
  if (sidebarFilter === 'nosummary') result = result.filter(c => !c.has_summary);
  if (sidebarFilter === 'fav')       result = result.filter(c => isFavorite(c.path));
  if (sidebarSort === 'files')    result.sort((a, b) => b.file_count - a.file_count);
  if (sidebarSort === 'progress') result.sort((a, b) => {
    const pa = a.progress.total ? a.progress.known / a.progress.total : 0;
    const pb = b.progress.total ? b.progress.known / b.progress.total : 0;
    return pb - pa;
  });
  if (sidebarSort === 'recent') result.sort((a, b) => (b.summary_age || 0) - (a.summary_age || 0));
  return result;
}

function filterAndRenderSidebar() {
  const q = document.getElementById('sidebar-search').value.toLowerCase();

  // Build filtered tree
  const filteredTree = [];
  for (const item of courseTree) {
    if (item.is_group) {
      const filtered = filterCourses(item.courses);
      // Also match if group name matches query
      if (filtered.length || item.name.toLowerCase().includes(q)) {
        filteredTree.push({ ...item, courses: filtered.length ? filtered : item.courses.filter(() => !q) });
      }
    } else {
      const matches = item.name.toLowerCase().includes(q);
      const passFilter = (sidebarFilter === 'all') ||
        (sidebarFilter === 'summary'   && item.has_summary) ||
        (sidebarFilter === 'nosummary' && !item.has_summary) ||
        (sidebarFilter === 'fav'       && isFavorite(item.path));
      if (matches && passFilter) filteredTree.push(item);
    }
  }

  renderSidebar(filteredTree);
}

// Track which groups are collapsed (default: open)
const collapsedGroups = new Set(JSON.parse(localStorage.getItem('collapsed_groups') || '[]'));

function toggleGroup(groupName) {
  if (collapsedGroups.has(groupName)) collapsedGroups.delete(groupName);
  else collapsedGroups.add(groupName);
  localStorage.setItem('collapsed_groups', JSON.stringify([...collapsedGroups]));
  filterAndRenderSidebar();
}

function renderSidebar(tree) {
  const el = document.getElementById('course-list');

  function courseHTML(c, indent = false) {
    const pct = c.progress.total ? Math.round(c.progress.known / c.progress.total * 100) : 0;
    const fav = isFavorite(c.path);
    const lastStudied = c.progress.last_studied
      ? new Date(c.progress.last_studied).toLocaleDateString('de-DE', {day:'2-digit', month:'2-digit'})
      : null;
    return `
    <div class="citem ${activeCourse === c.path ? 'active' : ''}" data-course="${esc(c.path)}" onclick="selectCourseFromEl(this)"
         style="${indent ? 'padding-left:22px' : ''}">
      <div class="citem-dot ${c.has_summary ? 'dot-ok' : 'dot-missing'}"></div>
      <div class="citem-body">
        <div class="citem-name" title="${esc(c.name)}">${esc(c.name)}${c.new_files ? `<span class="new-badge">+${c.new_files}</span>` : ''}</div>
        <div class="citem-meta">
          <span>${c.file_count} Datei${c.file_count !== 1 ? 'en' : ''}</span>
          ${c.has_summary ? `<span style="color:var(--green)">✓</span>` : ''}
          ${c.has_notes   ? `<span style="color:var(--purple)">📝</span>` : ''}
          ${lastStudied   ? `<span title="Zuletzt gelernt">🕐 ${lastStudied}</span>` : ''}
        </div>
        ${c.progress.total ? `<div class="progress-mini"><div class="progress-mini-fill" style="width:${pct}%"></div></div>` : ''}
      </div>
      <button class="fav-btn ${fav ? 'is-fav' : ''}" title="${fav ? 'Favorit entfernen' : 'Als Favorit markieren'}"
        onclick="event.stopPropagation(); handleFavClick('${esc(c.path)}')">⭐</button>
    </div>`;
  }

  function groupHTML(item) {
    const collapsed = collapsedGroups.has(item.name);
    const total    = item.courses.length;
    const withSum  = item.courses.filter(c => c.has_summary).length;
    const semClass = item.is_semester ? 'group-header semester-header' : 'group-header';
    const icon     = item.is_semester ? '🎓 ' : '';
    return `
    <div class="${semClass}" onclick="toggleGroup('${esc(item.name)}')">
      <span class="group-chevron">${collapsed ? '▶' : '▼'}</span>
      <span class="group-name">${icon}${esc(item.name)}</span>
      <span class="group-meta">${withSum}/${total}</span>
    </div>
    ${collapsed ? '' : item.courses.map(c => courseHTML(c, true)).join('')}`;
  }

  // Separate favorites from tree
  const favPaths = getFavorites();
  const favCourses = allCourses.filter(c => favPaths.includes(c.path));

  let html = '';
  if (favCourses.length) {
    html += `<div class="sidebar-section-label">⭐ Favoriten</div>`;
    html += favCourses.map(c => courseHTML(c)).join('');
    html += `<div class="sidebar-section-label" style="margin-top:6px">Kurse</div>`;
  } else {
    html += `<div class="sidebar-section-label">Kurse</div>`;
  }

  if (!tree.length) {
    html += `<div style="color:var(--text3);font-size:12px;padding:16px 12px">Keine Kurse gefunden</div>`;
  } else {
    for (const item of tree) {
      html += item.is_group ? groupHTML(item) : courseHTML(item);
    }
  }

  el.innerHTML = html;
}

function selectCourseFromEl(el) { selectCourse(el.dataset.course); }

function handleFavClick(path) {
  toggleFavorite(path);
  filterAndRenderSidebar();
}

// ═══════════════════════════════════════════════════════════════════════════
// Home
// ═══════════════════════════════════════════════════════════════════════════
function renderHome(courses, pipeline, streak) {
  const total      = courses.length;
  const withSum    = courses.filter(c => c.has_summary).length;
  const totalFiles = courses.reduce((a, c) => a + c.file_count, 0);
  const totalQ     = courses.reduce((a, c) => a + c.progress.total, 0);
  const totalKnown = courses.reduce((a, c) => a + c.progress.known, 0);
  const masteryPct = totalQ ? Math.round(totalKnown / totalQ * 100) : 0;
  const summaryPct = total ? Math.round(withSum / total * 100) : 0;

  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card" style="--accent:var(--blue)">
      <div class="stat-label">📚 Kurse</div>
      <div class="stat-value">${total}</div>
      <div class="stat-sub">${withSum} mit Zusammenfassung</div>
      <div class="stat-bar"><div class="stat-bar-fill" style="width:${summaryPct}%;background:linear-gradient(90deg,var(--blue),#60a5fa)"></div></div>
    </div>
    <div class="stat-card" style="--accent:var(--green)">
      <div class="stat-label">📁 Dateien gesamt</div>
      <div class="stat-value">${totalFiles}</div>
      <div class="stat-sub">in allen Kursen</div>
    </div>
    <div class="stat-card" style="--accent:var(--yellow)">
      <div class="stat-label">🃏 Lernkarten</div>
      <div class="stat-value">${totalQ}</div>
      <div class="stat-sub">${totalKnown} bekannt · ${totalQ - totalKnown} offen</div>
      ${totalQ ? `<div class="stat-bar"><div class="stat-bar-fill" style="width:${masteryPct}%;background:linear-gradient(90deg,var(--yellow),var(--orange))"></div></div>` : ''}
    </div>
    <div class="stat-card" style="--accent:var(--purple)">
      <div class="stat-label">🎯 Lernfortschritt</div>
      <div class="stat-value">${masteryPct}<span style="font-size:18px;font-weight:500;color:var(--text3)">%</span></div>
      <div class="stat-sub">Gesamt-Mastery</div>
      <div class="stat-bar"><div class="stat-bar-fill" style="width:${masteryPct}%;background:linear-gradient(90deg,var(--blue),var(--purple))"></div></div>
    </div>
    <div class="stat-card" style="--accent:var(--orange)">
      <div class="stat-label">🔥 Lernsträhne</div>
      <div class="stat-value">${streak?.count || 0}<span style="font-size:18px;font-weight:500;color:var(--text3)"> Tage</span></div>
      <div class="stat-sub">${streak?.last_date ? 'Zuletzt: ' + new Date(streak.last_date).toLocaleDateString('de-DE') : 'Noch nicht gelernt'}</div>
    </div>`;

  const pOk = pipeline.last_ok;
  const pending = allCourses.filter(c => !c.has_summary && c.file_count > 0).length;
  document.getElementById('pipeline-card-wrap').innerHTML = `
    <div class="pipeline-card" style="margin-bottom:24px">
      <div class="pipeline-dot" style="background:${pOk ? 'var(--green)' : 'var(--text3)'}"></div>
      <div>
        <div style="font-size:13px;font-weight:600;color:var(--text)">Pipeline</div>
        <div style="font-size:12px;color:var(--text3);margin-top:3px">
          ${pOk ? `Letzter Lauf: ${pOk}` : 'Noch nie gelaufen'}
          &nbsp;·&nbsp; Mo + Do 08:00
        </div>
      </div>
      <div style="flex:1"></div>
      <select id="global-lang-select" onchange="localStorage.setItem('summary_lang',this.value);toast('Sprache gespeichert','ok')" style="font-size:12px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text2);padding:5px 8px;outline:none;cursor:pointer" title="Sprache für Zusammenfassungen">
        <option value="en" ${(localStorage.getItem('summary_lang')||'en')==='en'?'selected':''}>🇬🇧 English</option>
        <option value="de" ${(localStorage.getItem('summary_lang')||'en')==='de'?'selected':''}>🇩🇪 Deutsch</option>
      </select>
      ${pending > 0 ? `<button class="tbtn btn-green" onclick="runBulkSummarize()" title="${pending} Kurse ohne Zusammenfassung">✨ Alle zusammenfassen (${pending})</button>` : ''}
      <button class="tbtn btn-purple" onclick="startGlobalLearn()">🧠 Alle lernen</button>
      <button class="tbtn btn-gray" onclick="runScraper()">↓ Sync</button>
    </div>`;

  const recent = [...courses]
    .filter(c => c.summary_age)
    .sort((a, b) => b.summary_age - a.summary_age)
    .slice(0, 8);

  // Recommendations
  const recs = buildRecommendations(courses);
  const recWrap = document.getElementById('recommendations-wrap');
  if (recs.length) {
    recWrap.innerHTML = `
      <div class="section-title" style="margin-bottom:10px">🎯 Empfohlen</div>
      <div class="rec-list">${recs.map(r => `
        <div class="rec-card" data-course="${esc(r.path)}" onclick="selectCourseFromEl(this)" style="--accent:${r.accent||'var(--border2)'}">
          <div class="rec-card-reason">${esc(r.reason)}</div>
          <div class="rec-card-name">${esc(r.name)}</div>
          <div class="rec-card-meta">${esc(r.meta)}</div>
        </div>`).join('')}
      </div>`;
  } else {
    recWrap.innerHTML = '';
  }

  document.getElementById('recent-list').innerHTML = recent.length ? recent.map(c => {
    const pct = c.progress.total ? Math.round(c.progress.known / c.progress.total * 100) : null;
    return `
    <div class="recent-item" data-course="${esc(c.path)}" onclick="selectCourseFromEl(this)">
      <span style="font-size:20px">${c.has_notes ? '📝' : '📚'}</span>
      <div class="recent-item-name">${esc(c.name)}</div>
      <div class="recent-item-meta">${c.file_count} Dateien</div>
      ${pct !== null ? `
        <div class="recent-item-progress">
          <div style="font-size:10px;color:var(--text3);text-align:right">${c.progress.known}/${c.progress.total}</div>
          <div class="recent-progress-bar"><div class="recent-progress-fill" style="width:${pct}%"></div></div>
        </div>` : ''}
    </div>`;
  }).join('') : '<div style="color:var(--text3);font-size:13px">Noch keine Zusammenfassungen erstellt</div>';
}

function buildRecommendations(courses) {
  const recs = [];
  const withSummary = courses.filter(c => c.has_summary);

  // 1. Kurse mit neuen Dateien seit letzter Zusammenfassung
  const newFiles = courses.filter(c => c.new_files > 0);
  if (newFiles.length) {
    const c = newFiles.sort((a, b) => b.new_files - a.new_files)[0];
    recs.push({ path: c.path, name: c.name, reason: '📂 Neue Dateien', meta: `${c.new_files} neue Datei${c.new_files > 1 ? 'en' : ''} seit letzter Zusammenfassung`, accent: 'var(--yellow)' });
  }

  // 2. Kurs mit niedrigstem Lernfortschritt (der Lernkarten hat)
  const withProgress = withSummary.filter(c => c.progress.total > 0);
  if (withProgress.length) {
    const c = withProgress.sort((a, b) => {
      const pa = a.progress.known / a.progress.total;
      const pb = b.progress.known / b.progress.total;
      return pa - pb;
    })[0];
    const pct = Math.round(c.progress.known / c.progress.total * 100);
    if (pct < 80) recs.push({ path: c.path, name: c.name, reason: '📉 Lernrückstand', meta: `${pct}% bekannt (${c.progress.known}/${c.progress.total} Karten)`, accent: 'var(--red)' });
  }

  // 3. Kurs ohne Zusammenfassung aber mit Dateien
  const noSummary = courses.filter(c => !c.has_summary && c.file_count > 0);
  if (noSummary.length) {
    const c = noSummary.sort((a, b) => b.file_count - a.file_count)[0];
    recs.push({ path: c.path, name: c.name, reason: '⚠️ Keine Zusammenfassung', meta: `${c.file_count} Datei${c.file_count > 1 ? 'en' : ''} vorhanden`, accent: 'var(--orange)' });
  }

  // 4. Am längsten nicht gelernt (aus denen mit Lernkarten)
  const withLast = withProgress.filter(c => c.progress.last_studied);
  if (withLast.length) {
    const c = withLast.sort((a, b) => new Date(a.progress.last_studied) - new Date(b.progress.last_studied))[0];
    const days = Math.floor((Date.now() - new Date(c.progress.last_studied)) / 86400000);
    if (days >= 3) recs.push({ path: c.path, name: c.name, reason: '⏰ Lange nicht gelernt', meta: `Vor ${days} Tag${days > 1 ? 'en' : ''} zuletzt geübt`, accent: 'var(--purple)' });
  }

  return recs.slice(0, 3);
}

function goHome() {
  activeCourse = null;
  document.getElementById('tabs').style.display = 'none';
  showPanel('home');
  filterAndRenderSidebar();
}

// ═══════════════════════════════════════════════════════════════════════════
// Course selection
// ═══════════════════════════════════════════════════════════════════════════
async function selectCourse(path) {
  activeCourse = path;
  filterAndRenderSidebar();
  document.getElementById('tabs').style.display = 'flex';
  // Show only the leaf course name (after last /)
  const displayName = path.split('/').pop();
  document.getElementById('tabs-course-label').textContent = displayName;
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
  if (tab === 'chat')    loadChat();
}

function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
}

// ═══════════════════════════════════════════════════════════════════════════
// Files tab
// ═══════════════════════════════════════════════════════════════════════════
async function loadFiles() {
  const [files, meta] = await Promise.all([
    fetch(`/api/files/${enc(activeCourse)}`).then(r => r.json()),
    fetch(`/api/file-meta/${enc(activeCourse)}`).then(r => r.json()).catch(() => []),
  ]);
  const el = document.getElementById('file-list');
  if (!files.length) {
    el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px">Keine Dateien</div>';
    return;
  }
  const metaMap = Object.fromEntries((meta || []).map(m => [m.name, m]));
  // Find summary age for highlighting new files
  const course = allCourses.find(c => c.path === activeCourse);
  const summaryAge = course?.summary_age || 0;

  // New-files banner
  const newCount = course?.new_files || 0;
  const banner = newCount > 0
    ? `<div style="margin-bottom:8px;padding:7px 10px;background:rgba(234,179,8,.1);border:1px solid rgba(234,179,8,.3);border-radius:6px;font-size:11px;color:var(--yellow)">
        ⚠ ${newCount} neue Datei${newCount>1?'en':''} seit letzter Zusammenfassung
       </div>`
    : '';

  el.innerHTML = banner + files.map(f => {
    const m = metaMap[f] || {};
    const isNew = summaryAge && m.mtime && m.mtime > summaryAge;
    const sizeStr = m.size ? (m.size > 1048576 ? (m.size/1048576).toFixed(1)+'MB' : Math.round(m.size/1024)+'KB') : '';
    return `
    <div class="file-item${isNew ? ' new-file' : ''}" id="fi-${CSS.escape(f)}" data-filename="${esc(f)}" onclick="previewFileFromEl(this)">
      <input type="checkbox" name="file" value="${esc(f)}" checked onclick="event.stopPropagation()">
      <span class="file-icon">${fileIcon(f)}</span>
      <span class="file-name" title="${esc(f)}">${esc(f)}${isNew ? '<span class="new-badge">Neu</span>' : ''}</span>
      ${sizeStr ? `<span class="file-meta-info">${sizeStr}</span>` : ''}
    </div>`;
  }).join('');
}

function previewFileFromEl(el) { previewFile(el.dataset.filename); }

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  return {pdf:'📕', docx:'📘', pptx:'📙', txt:'📃', md:'📝'}[ext] || '📄';
}

async function previewFile(filename) {
  document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
  const match = document.querySelector(`.file-item[data-filename="${CSS.escape(filename)}"]`);
  if (match) match.classList.add('active');

  const ext = filename.split('.').pop().toLowerCase();
  const header = document.getElementById('preview-header');
  const body   = document.getElementById('preview-body');

  header.innerHTML = `
    <span class="preview-header-name">${esc(filename)}</span>
    <a href="/api/file-raw/${enc(activeCourse)}/${enc(filename)}" download title="Herunterladen"
       style="color:var(--text3);font-size:13px;text-decoration:none" onclick="event.stopPropagation()">⬇</a>`;
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
        <button class="tbtn btn-gray" onclick="copyToClipboard(summaryMD)" title="Markdown kopieren">📋 Kopieren</button>
        <a class="tbtn btn-gray" href="/api/summary-raw/${enc(activeCourse)}" download style="text-decoration:none">⬇ Download</a>
        <button class="tbtn btn-gray" onclick="switchTab('files')">Neu erstellen</button>
      </div>
      <div class="md-content">${data.html}</div>`;
    window.summaryMD = data.md;
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
  const lang  = localStorage.getItem('summary_lang') || 'en';
  setLoading(true);
  logShow(`Generiere Zusammenfassung für "${activeCourse}"…\n`);

  const res  = await fetch('/api/summarize', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ course: activeCourse, limit, force, files, lang })
  });
  const data = await res.json();
  logAppend(data.log || '');
  setLoading(false);

  if (data.success) {
    logAppend('\n✅ Fertig!');
    courseTree = await fetch('/api/courses').then(r => r.json());
    allCourses = flattenTree(courseTree);
    filterAndRenderSidebar();
    switchTab('summary');
    toast('Zusammenfassung erstellt!', 'ok');
  } else {
    toast('Fehler beim Erstellen.', 'err');
  }
}

async function runBulkSummarize() {
  const lang = localStorage.getItem('summary_lang') || 'en';
  const btn = document.querySelector('[onclick="runBulkSummarize()"]');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Läuft…'; }
  logShow('Starte Zusammenfassung aller Kurse…\n');
  const res  = await fetch('/api/summarize-all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ lang })
  });
  const data = await res.json();
  logAppend(data.log || '');
  if (data.success) {
    logAppend('\n✅ Fertig!');
    courseTree = await fetch('/api/courses').then(r => r.json());
    allCourses = flattenTree(courseTree);
    filterAndRenderSidebar();
    toast(`${data.done} Zusammenfassungen erstellt!`, 'ok');
    // Re-render home to update pending count
    const streak = await fetch('/api/streak').then(r => r.json()).catch(() => null);
    renderHome(courseTree, null, streak);
  } else {
    toast('Fehler beim Zusammenfassen.', 'err');
  }
  if (btn) { btn.disabled = false; }
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('In Zwischenablage kopiert!', 'ok');
  } catch {
    toast('Kopieren nicht möglich.', 'err');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcards — single course
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

  flashState = { cards, index: 0, revealed: false, progress: prog.cards || {}, timerStart: Date.now(), timerInterval: null, isGlobal: false };
  startTimer();
  renderFlash();
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcards — global (all courses)
// ═══════════════════════════════════════════════════════════════════════════
async function startGlobalLearn() {
  const cards = await fetch('/api/all-flashcards').then(r => r.json());
  if (!cards.length) {
    toast('Noch keine Lernkarten vorhanden. Erstelle zuerst Zusammenfassungen.', 'err');
    return;
  }

  // Load all progress
  const prog = {};
  flashState = { cards: shuffleArr([...cards]), index: 0, revealed: false, progress: prog, timerStart: Date.now(), timerInterval: null, isGlobal: true };

  activeCourse = null;
  document.getElementById('tabs').style.display = 'none';
  filterAndRenderSidebar();
  showPanel('learn');
  startTimer();
  renderFlash();
}

function shuffleArr(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcard rendering
// ═══════════════════════════════════════════════════════════════════════════
function startTimer() {
  if (flashState.timerInterval) clearInterval(flashState.timerInterval);
  flashState.timerStart = Date.now();
  flashState.timerInterval = setInterval(() => {
    const el = document.getElementById('flash-timer-display');
    if (el) el.textContent = formatTime(Date.now() - flashState.timerStart);
  }, 1000);
}

function stopTimer() {
  if (flashState.timerInterval) { clearInterval(flashState.timerInterval); flashState.timerInterval = null; }
}

function formatTime(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

function renderFlash() {
  const { cards, index, progress, isGlobal } = flashState;
  const known   = Object.values(progress).filter(v => v === 'known').length;
  const unknown = Object.values(progress).filter(v => v === 'unknown').length;
  const done    = known + unknown;
  const pct     = cards.length ? Math.round(done / cards.length * 100) : 0;

  if (index >= cards.length) {
    stopTimer();
    const elapsed = formatTime(Date.now() - flashState.timerStart);
    document.getElementById('learn-body').innerHTML = `
      <div class="flash-layout">
        <div class="flash-done" style="display:flex">
          <div class="big-icon">🎉</div>
          <h2>Alle Karten durch!</h2>
          <p>${known} gewusst · ${unknown} nicht gewusst · ${cards.length} gesamt · ⏱ ${elapsed}</p>
          <div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;justify-content:center">
            <button class="tbtn btn-gray" onclick="restartFlash()">Nochmal</button>
            <button class="tbtn btn-blue" onclick="shuffleAndRestart()">🔀 Zufällig nochmal</button>
            ${unknown > 0 ? `<button class="tbtn btn-red" onclick="restartFlashUnknown()">Nur falsche (${unknown})</button>` : ''}
            ${!isGlobal ? `<button class="tbtn btn-gray" onclick="resetProgress()">Fortschritt zurücksetzen</button>` : ''}
          </div>
        </div>
      </div>`;
    if (!isGlobal) saveFlashProgress();
    return;
  }

  const card = cards[index];
  document.getElementById('learn-body').innerHTML = `
    <div class="flash-layout">
      <div class="flash-header">
        <span class="flash-header-title">${isGlobal ? '🌍 Alle Kurse' : esc(activeCourse || '')}</span>
        ${!isGlobal ? `<button class="tbtn btn-gray" style="font-size:11px;padding:4px 10px" onclick="resetProgress()">↺ Reset</button>` : ''}
        <button class="tbtn btn-gray" style="font-size:11px;padding:4px 10px" onclick="shuffleAndRestart()">🔀 Shuffle</button>
      </div>
      <div class="flash-progress-bar">
        <div class="flash-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="flash-meta">
        <span>${index + 1} / ${cards.length}</span>
        <span style="color:var(--green)">✓ ${known}</span>
        <span style="color:var(--red)">✗ ${unknown}</span>
        <span class="flash-timer" id="flash-timer-display">0:00</span>
      </div>
      <div class="flash-card" id="flash-card-el">
        ${isGlobal && card.course ? `<div class="flash-course-badge">${esc(card.course)}</div>` : ''}
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

  // Update timer display immediately
  const timerEl = document.getElementById('flash-timer-display');
  if (timerEl) timerEl.textContent = formatTime(Date.now() - flashState.timerStart);
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

  // Visual feedback
  const cardEl = document.getElementById('flash-card-el');
  if (cardEl) {
    cardEl.classList.add(rating === 'known' ? 'flash-card-known' : 'flash-card-unknown');
    setTimeout(() => renderFlash(), 120);
  } else {
    renderFlash();
  }

  if (!flashState.isGlobal) saveFlashProgress();
}

function saveFlashProgress() {
  const known = Object.values(flashState.progress).filter(v => v === 'known').length;
  fetch(`/api/progress/${enc(activeCourse)}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ total: flashState.cards.length, known, cards: flashState.progress })
  });
  allCourses = allCourses.map(c => c.path === activeCourse ? {...c, progress: {total: flashState.cards.length, known}} : c);
  filterAndRenderSidebar();
}

function restartFlash() {
  flashState.index = 0;
  flashState.progress = {};
  flashState.timerStart = Date.now();
  renderFlash();
}

function shuffleAndRestart() {
  flashState.cards = shuffleArr([...flashState.cards]);
  flashState.index = 0;
  flashState.progress = {};
  flashState.timerStart = Date.now();
  startTimer();
  renderFlash();
}

function restartFlashUnknown() {
  const unknown = flashState.cards.filter(c => flashState.progress[c.id] !== 'known');
  flashState = { ...flashState, cards: shuffleArr(unknown), index: 0, revealed: false, progress: {} };
  flashState.timerStart = Date.now();
  startTimer();
  renderFlash();
}

function resetProgress() {
  showConfirm(
    'Lernfortschritt zurücksetzen?',
    `Alle Fortschrittsdaten für "${activeCourse}" werden gelöscht.`,
    async () => {
      await fetch(`/api/progress-reset/${enc(activeCourse)}`, { method: 'POST' });
      flashState.progress = {};
      allCourses = allCourses.map(c => c.name === activeCourse ? {...c, progress: {total: 0, known: 0}} : c);
      filterAndRenderSidebar();
      toast('Fortschritt zurückgesetzt', 'ok');
      loadFlashcards();
    }
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// Flashcard keyboard shortcuts
// ═══════════════════════════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  // Overlay close
  if (e.key === 'Escape') {
    if (document.getElementById('shortcuts-overlay').classList.contains('open')) { hideShortcuts(); return; }
    if (document.getElementById('confirm-overlay').classList.contains('open')) { hideConfirm(); return; }
    goHome();
    return;
  }

  if (e.key === '?' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
    showShortcuts(); return;
  }

  // Ctrl+K → focus search
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault();
    document.getElementById('search-global').focus();
    return;
  }

  // Tab shortcuts (1-4) when a course is active
  if (activeCourse && !e.ctrlKey && !e.metaKey && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
    if (e.key === '1') { switchTab('files'); return; }
    if (e.key === '2') { switchTab('summary'); return; }
    if (e.key === '3') { switchTab('learn'); return; }
    if (e.key === '4') { switchTab('notes'); return; }
    if (e.key === '5') { switchTab('chat'); return; }
  }

  // Flashcard shortcuts
  if (activeTab !== 'learn' && document.getElementById('panel-learn').style.display === 'none') return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

  if (e.key === ' ' || e.code === 'Space') {
    e.preventDefault();
    if (!flashState.revealed && flashState.index < flashState.cards.length) revealFlash();
  } else if (e.key === 'ArrowRight' || e.key === 'k') {
    e.preventDefault();
    if (flashState.revealed && flashState.index < flashState.cards.length) rateFlash('known');
  } else if (e.key === 'ArrowLeft' || e.key === 'u') {
    e.preventDefault();
    if (flashState.revealed && flashState.index < flashState.cards.length) rateFlash('unknown');
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Notes
// ═══════════════════════════════════════════════════════════════════════════
async function loadNotes() {
  const data = await fetch(`/api/notes/${enc(activeCourse)}`).then(r => r.json());
  const editor = document.getElementById('notes-editor');
  editor.value = data.text || '';
  document.getElementById('notes-saved').style.display = 'none';
  // Reset preview mode
  if (notesPreviewMode) toggleNotesPreview();
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
  allCourses = allCourses.map(c => c.path === activeCourse ? {...c, has_notes: true} : c);
}

function toggleNotesPreview() {
  notesPreviewMode = !notesPreviewMode;
  const editor  = document.getElementById('notes-editor');
  const preview = document.getElementById('notes-preview');
  const btn     = document.getElementById('notes-preview-btn');

  if (notesPreviewMode) {
    // Render markdown preview
    const md = editor.value;
    // Simple client-side markdown: use server for rendering
    fetch('/api/notes-preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text: md })
    }).then(r => r.json()).then(d => {
      preview.innerHTML = d.html || '';
    }).catch(() => {
      // Fallback: basic rendering
      preview.innerHTML = '<pre style="color:var(--text2);white-space:pre-wrap">' + esc(md) + '</pre>';
    });
    editor.style.display = 'none';
    preview.style.display = 'block';
    btn.textContent = 'Bearbeiten';
  } else {
    editor.style.display = 'block';
    preview.style.display = 'none';
    btn.textContent = 'Vorschau';
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Chat / Ask AI
// ═══════════════════════════════════════════════════════════════════════════
let chatHistory = [];   // [{role, content}]
let chatStreaming = false;

const CHAT_SUGGESTIONS = [
  'Was sind die wichtigsten Konzepte?',
  'Erkläre den wichtigsten Begriff einfach.',
  'Welche Prüfungsfragen könnten kommen?',
  'Fasse das Wichtigste in 3 Stichpunkten zusammen.',
  'Was hängt mit diesem Thema zusammen?',
];

function loadChat() {
  chatHistory = [];
  const course = allCourses.find(c => c.path === activeCourse);
  const hasSummary = course?.has_summary;
  document.getElementById('chat-body').innerHTML = `
    <div class="chat-layout">
      <div class="chat-messages" id="chat-messages">
        <div class="chat-msg">
          <div class="chat-avatar">🤖</div>
          <div class="chat-bubble">
            ${hasSummary
              ? `Hallo! Ich kenne die Zusammenfassung von <strong>${esc(activeCourse.split('/').pop())}</strong>. Was möchtest du wissen?`
              : `Für diesen Kurs gibt es noch keine Zusammenfassung. Erstelle zuerst eine unter „Dateien", damit ich dir gezielt helfen kann.`}
          </div>
        </div>
      </div>
      ${hasSummary ? `<div class="chat-suggestions" id="chat-suggestions">
        ${CHAT_SUGGESTIONS.map(s => `<button class="chat-suggestion" onclick="sendSuggestion('${esc(s)}')">${esc(s)}</button>`).join('')}
      </div>` : ''}
      <div class="chat-input-row">
        <textarea id="chat-input" placeholder="Frage stellen… (Enter zum Senden, Shift+Enter für Zeilenumbruch)"
          ${hasSummary ? '' : 'disabled'}
          onkeydown="handleChatKey(event)" oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
        <button class="chat-input-btn" id="chat-send-btn" onclick="sendChat()" ${hasSummary ? '' : 'disabled'}>Senden</button>
      </div>
    </div>`;
}

function sendSuggestion(text) {
  document.getElementById('chat-input').value = text;
  document.getElementById('chat-suggestions')?.remove();
  sendChat();
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

async function sendChat() {
  if (chatStreaming) return;
  const input = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question) return;

  input.value = '';
  input.style.height = 'auto';
  document.getElementById('chat-suggestions')?.remove();

  // Add user message
  appendChatMsg('user', question);
  chatHistory.push({ role: 'user', content: question });

  // Add empty AI bubble with streaming cursor
  const aiId = 'ai-msg-' + Date.now();
  const messagesEl = document.getElementById('chat-messages');
  const bubble = document.createElement('div');
  bubble.className = 'chat-msg';
  bubble.innerHTML = `<div class="chat-avatar">🤖</div><div class="chat-bubble streaming" id="${aiId}"></div>`;
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  chatStreaming = true;
  document.getElementById('chat-send-btn').disabled = true;
  input.disabled = true;

  let fullAnswer = '';
  try {
    const resp = await fetch(`/api/chat/${enc(activeCourse)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history: chatHistory.slice(0, -1) })
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    const bubbleEl = document.getElementById(aiId);

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const { delta } = JSON.parse(data);
          fullAnswer += delta;
          bubbleEl.textContent = fullAnswer;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } catch {}
      }
    }

    const bubbleEl2 = document.getElementById(aiId);
    if (bubbleEl2) {
      bubbleEl2.classList.remove('streaming');
      // Simple markdown: bold, newlines
      bubbleEl2.innerHTML = fullAnswer
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>');
      bubbleEl2.innerHTML = '<p>' + bubbleEl2.innerHTML + '</p>';
    }
    chatHistory.push({ role: 'assistant', content: fullAnswer });
  } catch (err) {
    const bubbleEl = document.getElementById(aiId);
    if (bubbleEl) { bubbleEl.classList.remove('streaming'); bubbleEl.textContent = 'Fehler: ' + err.message; }
  }

  chatStreaming = false;
  document.getElementById('chat-send-btn').disabled = false;
  input.disabled = false;
  input.focus();
}

function appendChatMsg(role, text) {
  const el = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  div.innerHTML = `
    <div class="chat-avatar">${role === 'user' ? '🧑' : '🤖'}</div>
    <div class="chat-bubble">${esc(text)}</div>`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

// Auto-save notes + unsaved indicator
document.addEventListener('input', e => {
  if (e.target.id === 'notes-editor') {
    const el = document.getElementById('notes-saved');
    el.textContent = '● Ungespeichert';
    el.style.color = 'var(--orange)';
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
  const escaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const highlighted = results.map(r => {
    const snippet = r.snippet.replace(new RegExp(escaped, 'gi'), m => `<mark>${m}</mark>`);
    return `
      <div class="search-result" data-course="${esc(r.course)}" onclick="selectCourseFromEl(this);switchTab(r.source==='notes'?'notes':'summary')">
        <div class="search-result-course">
          ${esc(r.name || r.course)}
          ${r.count > 1 ? `<span class="search-result-count">${r.count} Treffer</span>` : ''}
          ${r.source ? `<span class="search-source search-source-${r.source}">${r.source==='both'?'Zusammenfassung + Notizen':r.source==='notes'?'Notizen':'Zusammenfassung'}</span>` : ''}
        </div>
        <div class="search-result-snippet">…${snippet}…</div>
      </div>`;
  }).join('');
  el.innerHTML = `<div class="search-results-header">${results.length} Kurse mit Treffern für „${esc(q)}"</div>${highlighted}`;
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
    courseTree = await fetch('/api/courses').then(r => r.json());
    allCourses = flattenTree(courseTree);
    filterAndRenderSidebar();
    toast('Dateien aktualisiert!', 'ok');
  } else {
    toast('Fehler beim Scraping.', 'err');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Resizable dividers
// ═══════════════════════════════════════════════════════════════════════════
function initResizeDividers() {
  const savedSidebar = localStorage.getItem('sidebar_width');
  if (savedSidebar) document.getElementById('sidebar').style.width = savedSidebar + 'px';
  const savedFilesCol = localStorage.getItem('files_col_width');
  if (savedFilesCol) document.getElementById('files-list-col').style.width = savedFilesCol + 'px';

  setupColResize(document.getElementById('divider-sidebar'),  document.getElementById('sidebar'),       140, 480, 'sidebar_width');
  setupColResize(document.getElementById('divider-files'),    document.getElementById('files-list-col'), 120, 500, 'files_col_width');
}

function setupColResize(divider, targetEl, minW, maxW, storageKey) {
  let startX, startW;
  divider.addEventListener('mousedown', e => {
    e.preventDefault();
    startX = e.clientX;
    startW = targetEl.getBoundingClientRect().width;
    divider.classList.add('dragging');

    function onMove(e) {
      const newW = Math.min(maxW, Math.max(minW, startW + e.clientX - startX));
      targetEl.style.width = newW + 'px';
      if (storageKey === 'sidebar_width') targetEl.style.minWidth = newW + 'px';
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
// Toast notifications
// ═══════════════════════════════════════════════════════════════════════════
function toast(msg, type = '') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast${type ? ' toast-' + type : ''}`;
  el.innerHTML = `${type === 'ok' ? '✓' : type === 'err' ? '✗' : 'ℹ'} ${esc(msg)}`;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, 2500);
}

// ═══════════════════════════════════════════════════════════════════════════
// Shortcuts overlay
// ═══════════════════════════════════════════════════════════════════════════
function showShortcuts() { document.getElementById('shortcuts-overlay').classList.add('open'); }
function hideShortcuts() { document.getElementById('shortcuts-overlay').classList.remove('open'); }

// ═══════════════════════════════════════════════════════════════════════════
// Confirm modal
// ═══════════════════════════════════════════════════════════════════════════
let confirmCallback = null;
function showConfirm(title, message, callback) {
  document.getElementById('confirm-title').textContent   = title;
  document.getElementById('confirm-message').textContent = message;
  confirmCallback = callback;
  document.getElementById('confirm-overlay').classList.add('open');
  document.getElementById('confirm-ok').onclick = () => { hideConfirm(); callback(); };
}
function hideConfirm() { document.getElementById('confirm-overlay').classList.remove('open'); }

// ═══════════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════════
function setLoading(on) {
  document.getElementById('tab-spinner').style.display = on ? 'inline-block' : 'none';
  document.querySelectorAll('.tbtn, .flash-btn').forEach(b => b.disabled = on);
}

function logShow(text) {
  const box = document.getElementById('log-box');
  box.style.display = 'flex';
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

@app.route("/api/chat/<path:course_name>", methods=["POST"])
def api_chat(course_name):
    from anthropic import Anthropic
    from flask import Response, stream_with_context
    question = request.json.get("question", "").strip()
    history  = request.json.get("history", [])   # [{role, content}, …]
    if not question:
        return jsonify({"error": "Keine Frage"}), 400

    course_dir   = COURSES_DIR / course_name
    summary_path = course_dir / OUTPUT_FILENAME
    notes_path   = course_dir / NOTES_FILENAME
    context = summary_path.read_text(encoding="utf-8")[:10000] if summary_path.exists() else ""
    notes   = notes_path.read_text(encoding="utf-8")[:3000]    if notes_path.exists()   else ""

    system = f"""Du bist ein präziser Lernassistent für den Kurs „{course_name.split('/')[-1]}". \
Antworte immer auf Deutsch, knapp und lernorientiert.

<kurszusammenfassung>
{context or "Keine Zusammenfassung vorhanden."}
</kurszusammenfassung>
{f"<notizen>{notes}</notizen>" if notes else ""}

Beantworte Fragen ausschließlich basierend auf diesen Materialien. \
Wenn etwas nicht abgedeckt ist, weise darauf hin."""

    messages = history + [{"role": "user", "content": question}]

    def generate():
        client = Anthropic()
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'delta': text})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/file-meta/<path:course_name>")
def api_file_meta(course_name):
    d = COURSES_DIR / course_name
    if not d.is_dir():
        return jsonify([])
    result = []
    for fname in list_files(d):
        p = d / fname
        if p.exists():
            result.append({"name": fname, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
    return jsonify(result)

@app.route("/api/notes-preview", methods=["POST"])
def api_notes_preview():
    text = request.json.get("text", "")
    html = markdown2.markdown(text, extras=["fenced-code-blocks", "tables"])
    return jsonify({"html": html})

@app.route("/")
def index():
    return render_template_string(HTML)

if __name__ == "__main__":
    print("Dashboard → http://localhost:5001")
    app.run(debug=True, port=5001)
