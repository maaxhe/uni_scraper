"""
Summarizer: liest heruntergeladene Stud.IP-Dateien, erstellt pro Kurs eine
einzige Zusammenfassungs-Markdown-Datei mit Trainingsfragen via Claude API.

Usage:
    python summarize.py                        # alle Kurse (max 3 Dateien pro Kurs)
    python summarize.py --course italienisch   # Kurs per Teilname angeben
    python summarize.py --course italienisch --limit 5
    python summarize.py --course italienisch --force   # neu generieren
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import anthropic
import fitz  # PyMuPDF
from docx import Document
from dotenv import load_dotenv

load_dotenv()

COURSES_DIR = Path("/Users/maxmacbookpro/Documents/Uni/Cognitive Science [Course]/Courses")
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_CHARS = 10_000
MODEL = "claude-opus-4-6"
OUTPUT_FILENAME = "_zusammenfassung.md"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            doc = fitz.open(str(path))
            return "\n".join(page.get_text() for page in doc)
        elif suffix == ".docx":
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        elif suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning("Konnte Text nicht lesen aus %s: %s", path.name, exc)
    return ""


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def summarize_files(client: anthropic.Anthropic, course_name: str, files: list[dict]) -> str:
    """
    Sends multiple file contents to Claude and gets back one combined
    Markdown document with summaries and training questions per file.
    """
    files_block = ""
    for i, f in enumerate(files, 1):
        text = f["text"][:MAX_CHARS]
        if len(f["text"]) > MAX_CHARS:
            text += "\n\n[... Text gekürzt ...]"
        files_block += f"\n\n### Datei {i}: {f['name']}\n\n{text}"

    prompt = f"""Du bist ein Lernassistent für Studierende der Cognitive Science an der Universität Osnabrück.

Kurs: {course_name}

Ich gebe dir {len(files)} Datei(en) aus diesem Kurs. Erstelle daraus ein strukturiertes Lernheft auf Deutsch.

Für jede Datei einen eigenen Abschnitt mit:

## [Dateiname]

**Zusammenfassung** (3-5 Sätze)

**Kernkonzepte**
- Konzept 1: kurze Erklärung
- Konzept 2: kurze Erklärung
- ...

**Trainingsfragen**
1. Frage?
2. Frage?
3. Frage?
4. Frage?
5. Frage?

**Antworten**
1. Antwort
2. Antwort
...

**Weiterführende Themen**
- Thema 1 — warum interessant
- Thema 2 — warum interessant
- Thema 3 — warum interessant

---

Hier die Dateien:{files_block}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Course processing
# ---------------------------------------------------------------------------

def find_course(name_query: str) -> Path | None:
    """Find a course folder by partial case-insensitive name match."""
    query = name_query.lower()
    matches = [d for d in COURSES_DIR.iterdir() if d.is_dir() and query in d.name.lower()]
    if not matches:
        return None
    if len(matches) > 1:
        log.warning("Mehrere Treffer für '%s': %s", name_query, [m.name for m in matches])
        log.warning("Nehme den ersten: %s", matches[0].name)
    return matches[0]


def process_course(client: anthropic.Anthropic, course_dir: Path, limit: int, force: bool, only_files: list[str] | None = None) -> None:
    output_path = course_dir / OUTPUT_FILENAME

    if output_path.exists() and not force:
        log.info("  ✓ Zusammenfassung existiert bereits: %s", output_path)
        log.info("  Nutze --force um neu zu generieren.")
        return

    # Collect files, skip existing summary files
    all_files = [
        f for f in course_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXTENSIONS
        and ".summary" not in f.name
        and f.name != OUTPUT_FILENAME
    ]

    if not all_files:
        log.info("  Keine Dateien gefunden.")
        return

    # Filter to specific files if requested
    if only_files:
        all_files = [f for f in all_files if f.name in only_files]

    # Apply limit
    files_to_process = all_files[:limit]
    if len(all_files) > limit:
        log.info("  %d Datei(en) gefunden, verarbeite %d (--limit %d)", len(all_files), limit, limit)
    else:
        log.info("  %d Datei(en) gefunden", len(all_files))

    # Extract text
    file_contents = []
    for f in files_to_process:
        text = extract_text(f)
        if text.strip():
            file_contents.append({"name": f.name, "text": text})
            log.info("  ✎ %s", f.name)
        else:
            log.warning("  Kein Text: %s — übersprungen", f.name)

    if not file_contents:
        log.warning("  Keine lesbaren Dateien.")
        return

    log.info("  → Sende %d Datei(en) an Claude…", len(file_contents))
    try:
        summary = summarize_files(client, course_dir.name, file_contents)
        header = (
            f"# Zusammenfassung: {course_dir.name}\n\n"
            f"*Generiert am {__import__('datetime').date.today()}*  \n"
            f"*{len(file_contents)} von {len(all_files)} Datei(en) zusammengefasst*\n\n"
            f"---\n\n"
        )
        output_path.write_text(header + summary, encoding="utf-8")
        log.info("  ✓ Gespeichert: %s", output_path)
    except Exception as exc:
        log.error("  Fehler: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Stud.IP Kurs-Zusammenfasser")
    parser.add_argument("--course", metavar="NAME", help="Kurs per Teilname angeben (z.B. 'italienisch')")
    parser.add_argument("--limit", type=int, default=3, help="Max. Dateien pro Kurs (Standard: 3)")
    parser.add_argument("--force", action="store_true", help="Bestehende Zusammenfassung überschreiben")
    parser.add_argument("--files", nargs="+", metavar="FILE", help="Nur diese Dateien zusammenfassen")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY fehlt in der .env Datei")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if args.course:
        course_dir = find_course(args.course)
        if not course_dir:
            log.error("Kein Kurs gefunden für: '%s'", args.course)
            log.info("Verfügbare Kurse:")
            for d in sorted(COURSES_DIR.iterdir()):
                if d.is_dir():
                    log.info("  %s", d.name)
            sys.exit(1)
        log.info("── Kurs: %s", course_dir.name)
        process_course(client, course_dir, args.limit, args.force, only_files=args.files)
    else:
        for course_dir in sorted(COURSES_DIR.iterdir()):
            if course_dir.is_dir():
                log.info("── Kurs: %s", course_dir.name)
                process_course(client, course_dir, args.limit, args.force)

    log.info("Fertig.")


if __name__ == "__main__":
    main()
