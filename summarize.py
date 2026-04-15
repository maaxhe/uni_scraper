"""
Summarizer: liest heruntergeladene Stud.IP-Dateien, erstellt pro Kurs eine
einzige Zusammenfassungs-Markdown-Datei mit Trainingsfragen via Claude API.

Usage:
    python summarize.py                                     # alle Kurse
    python summarize.py --course italienisch                # Kurs per Teilname
    python summarize.py --dir /abs/path/to/course          # direkter Pfad (Dashboard nutzt das)
    python summarize.py --course italienisch --limit 5
    python summarize.py --course italienisch --force        # neu generieren
    python summarize.py --lang en                          # Englisch (Standard)
    python summarize.py --lang de                          # Deutsch
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

COURSES_DIR = Path(os.environ.get("COURSES_DIR", "/Users/maxmacbookpro/Documents/Uni/Cognitive Science [Course]/Courses"))
SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md", ".pptx", ".ppt"}
MAX_CHARS = 40_000
MODEL_ANTHROPIC = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
MODEL_OPENAI    = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
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
        elif suffix in {".doc", ".docx"}:
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
    except Exception as exc:
        log.warning("Konnte Text nicht lesen aus %s: %s", path.name, exc)
    return ""


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

PROMPTS = {
    "en": {
        "intro": (
            "You are a concise study assistant for university students.\n\n"
            "Course: {course_name}\n\n"
            "I'm giving you {n} file(s). For each file produce a SHORT study note "
            "using ONLY bullet points — no prose paragraphs. Be dense and direct."
        ),
        "section": "## [Filename]",
        "summary_label": "**Summary** (3–5 bullets, one idea each)",
        "concepts_label": "**Key Concepts**",
        "questions_label": "**Training Questions**",
        "answers_label": "**Answers**",
        "header": "# Summary: {name}\n\n*Generated on {date}*  \n*{n} of {total} file(s) summarised*\n\n---\n\n",
        "truncated": "\n\n[... text truncated ...]",
        "file_label": "### File {i}: {name}",
    },
    "de": {
        "intro": (
            "Du bist ein prägnanter Lernassistent für Studierende.\n\n"
            "Kurs: {course_name}\n\n"
            "Ich gebe dir {n} Datei(en). Erstelle für jede Datei eine KURZE Lernnotiz "
            "ausschließlich in Stichpunkten — keine Fließtextabsätze. Knapp und präzise."
        ),
        "section": "## [Dateiname]",
        "summary_label": "**Zusammenfassung** (3–5 Bullets, je eine Kernaussage)",
        "concepts_label": "**Kernkonzepte**",
        "questions_label": "**Trainingsfragen**",
        "answers_label": "**Antworten**",
        "header": "# Zusammenfassung: {name}\n\n*Generiert am {date}*  \n*{n} von {total} Datei(en) zusammengefasst*\n\n---\n\n",
        "truncated": "\n\n[... Text gekürzt ...]",
        "file_label": "### Datei {i}: {name}",
    },
}


def summarize_files(client, client_type: str, course_name: str, files: list[dict], lang: str = "en") -> str:
    p = PROMPTS.get(lang, PROMPTS["en"])

    files_block = ""
    for i, f in enumerate(files, 1):
        text = f["text"][:MAX_CHARS]
        if len(f["text"]) > MAX_CHARS:
            text += p["truncated"]
        files_block += f"\n\n{p['file_label'].format(i=i, name=f['name'])}\n\n{text}"

    prompt = f"""{p['intro'].format(course_name=course_name, n=len(files))}

Use this exact structure for each file (all bullet points, no prose):

{p['section']}

{p['summary_label']}
- ...
- ...

{p['concepts_label']}
- **Term**: one-line definition
- **Term**: one-line definition

{p['questions_label']}
1. Question?
2. Question?
3. Question?

{p['answers_label']}
1. Answer (one sentence)
2. Answer (one sentence)
3. Answer (one sentence)

---

Files:{files_block}"""

    if client_type == "anthropic":
        try:
            response = client.messages.create(
                model=MODEL_ANTHROPIC,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError:
            log.error("ANTHROPIC_API_KEY ungültig – neuen Key unter https://console.anthropic.com erstellen.")
            sys.exit(1)
        except anthropic.APIConnectionError:
            log.error("Verbindung zur Anthropic API fehlgeschlagen – Internetverbindung prüfen.")
            sys.exit(1)
        return response.content[0].text
    else:  # openai-compatible (OpenAI, Groq, Mistral, Ollama, …)
        try:
            response = client.chat.completions.create(
                model=MODEL_OPENAI,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.error("API error: %s", e)
            sys.exit(1)
        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Course processing
# ---------------------------------------------------------------------------

def find_course(name_query: str) -> Path | None:
    """Find a course folder by partial case-insensitive name match (recursive 2 levels)."""
    query = name_query.lower()
    # First try top-level
    for d in COURSES_DIR.iterdir():
        if d.is_dir() and query in d.name.lower():
            return d
    # Then try one level deeper (groups)
    for group in COURSES_DIR.iterdir():
        if not group.is_dir():
            continue
        for d in group.iterdir():
            if d.is_dir() and query in d.name.lower():
                return d
    return None


def process_course(
    client,
    client_type: str,
    course_dir: Path,
    limit: int,
    force: bool,
    only_files: list[str] | None = None,
    lang: str = "en",
    output_filename: str | None = None,
) -> None:
    output_path = course_dir / (output_filename or OUTPUT_FILENAME)

    if output_path.exists() and not force:
        log.info("  ✓ Summary already exists: %s", output_path)
        log.info("  Use --force to regenerate.")
        return

    all_files = [
        f for f in course_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in SUPPORTED_EXTENSIONS
        and ".summary" not in f.name
        and f.name != OUTPUT_FILENAME
    ]

    if not all_files:
        log.info("  No files found.")
        return

    if only_files:
        only_set = set(only_files)
        all_files = [
            f for f in all_files
            if f.name in only_set or str(f.relative_to(course_dir)) in only_set
        ]

    files_to_process = all_files[:limit]
    if len(all_files) > limit:
        log.info("  %d file(s) found, processing %d (--limit %d)", len(all_files), limit, limit)
    else:
        log.info("  %d file(s) found", len(all_files))

    file_contents = []
    for f in files_to_process:
        text = extract_text(f)
        if text.strip():
            file_contents.append({"name": f.name, "text": text})
            log.info("  ✎ %s", f.name)
        else:
            log.warning("  No text: %s — skipped", f.name)

    if not file_contents:
        log.warning("  No readable files.")
        return

    log.info("  → Sending %d file(s) to AI…", len(file_contents))
    try:
        summary = summarize_files(client, client_type, course_dir.name, file_contents, lang=lang)
        p = PROMPTS.get(lang, PROMPTS["en"])
        header = p["header"].format(
            name=course_dir.name,
            date=__import__("datetime").date.today(),
            n=len(file_contents),
            total=len(all_files),
        )
        output_path.write_text(header + summary, encoding="utf-8")
        log.info("  ✓ Saved: %s", output_path)
    except Exception as exc:
        log.error("  Error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Stud.IP Course Summarizer")
    parser.add_argument("--course", metavar="NAME", help="Course name (partial match)")
    parser.add_argument("--dir", metavar="PATH", help="Absolute path to course directory (overrides --course)")
    parser.add_argument("--limit", type=int, default=3, help="Max files per course (default: 3)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing summary")
    parser.add_argument("--files", nargs="+", metavar="FILE", help="Only summarise these files")
    parser.add_argument("--lang", default="en", choices=["en", "de"], help="Summary language (default: en)")
    parser.add_argument("--out", metavar="FILENAME", default=None, help="Output filename (overrides default _zusammenfassung.md)")
    args = parser.parse_args()

    # ── Build AI client — Anthropic takes priority, then any OpenAI-compatible provider ──
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key    = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        client      = anthropic.Anthropic(api_key=anthropic_key)
        client_type = "anthropic"
        log.info("Using Anthropic (model: %s)", MODEL_ANTHROPIC)
    elif openai_key:
        try:
            from openai import OpenAI
        except ImportError:
            log.error("openai package not installed. Run: pip install openai")
            sys.exit(1)
        base_url    = os.environ.get("OPENAI_BASE_URL") or None
        client      = OpenAI(api_key=openai_key, base_url=base_url)
        client_type = "openai"
        log.info("Using OpenAI-compatible API (model: %s%s)", MODEL_OPENAI,
                 f", base_url: {base_url}" if base_url else "")
    else:
        log.error(
            "No API key found in .env.\n"
            "  Set ANTHROPIC_API_KEY  →  https://console.anthropic.com\n"
            "  or OPENAI_API_KEY      →  https://platform.openai.com/api-keys"
        )
        sys.exit(1)

    if args.dir:
        course_dir = Path(args.dir)
        if not course_dir.is_dir():
            log.error("Directory not found: %s", args.dir)
            sys.exit(1)
        log.info("── Course: %s", course_dir.name)
        process_course(client, client_type, course_dir, args.limit, args.force, only_files=args.files, lang=args.lang, output_filename=args.out)
    elif args.course:
        course_dir = find_course(args.course)
        if not course_dir:
            log.error("No course found for: '%s'", args.course)
            sys.exit(1)
        log.info("── Course: %s", course_dir.name)
        process_course(client, client_type, course_dir, args.limit, args.force, only_files=args.files, lang=args.lang, output_filename=args.out)
    else:
        for course_dir in sorted(COURSES_DIR.iterdir()):
            if course_dir.is_dir():
                log.info("── Course: %s", course_dir.name)
                process_course(client, client_type, course_dir, args.limit, args.force, lang=args.lang)

    log.info("Done.")


if __name__ == "__main__":
    main()
