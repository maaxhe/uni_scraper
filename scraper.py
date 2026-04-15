"""
Stud.IP scraper for the University of Osnabrück.
Downloads files from courses in the current semester or a specific course URL.

Usage:
    python scraper.py                       # scrape all courses in current semester
    python scraper.py --url <course_url>    # scrape a single course
    python scraper.py --output ./downloads  # custom output directory
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

STUDIP_BASE  = "https://studip.uni-osnabrueck.de"
COURSES_JSON = Path(__file__).parent / "courses.json"
LOGIN_URL = f"{STUDIP_BASE}/index.php"
MY_COURSES_URL = f"{STUDIP_BASE}/dispatch.php/my_courses"
# With sem_select=all Stud.IP shows every past semester, not just the current one.
MY_COURSES_ALL_URL = f"{STUDIP_BASE}/dispatch.php/my_courses?sem_select=all"

# Selectors for the Stud.IP login form on index.php.
# If login fails, open index.php in a browser, right-click the username field
# → Inspect, and confirm the input's name/id attributes match these selectors.
SSO_USERNAME_SELECTOR = 'input[name="loginname"], input[name="username"], input[id="loginname"]'
SSO_PASSWORD_SELECTOR = 'input[name="password"], input[id="password"]'
SSO_SUBMIT_SELECTOR   = 'input[name="Login"], button[type="submit"], input[type="submit"]'

SSO_TIMEOUT_MS = 30_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_dirname(name: str) -> str:
    """Strip characters that are invalid in directory names."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "unnamed_course"


def already_exists(dest_dir: Path, filename: str) -> bool:
    return (dest_dir / filename).exists()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

async def login(page: Page) -> None:
    """
    Navigate to my_courses, follow the SSO redirect and log in.

    ── Where to adjust selectors ──────────────────────────────────────────
    Uni Osnabrück uses Shibboleth/SAML via an external IdP. The login page
    URL usually looks like:
        https://sso.uni-osnabrueck.de/idp/...
    or:
        https://login.uni-osnabrueck.de/...

    1. SSO_USERNAME_SELECTOR / SSO_PASSWORD_SELECTOR / SSO_SUBMIT_SELECTOR
       Open the login page manually in a browser, right-click the username
       field → "Inspect", and copy its `name` or `id` attribute. Replace
       the fallback selectors at the top of this file accordingly.

    2. If there is a "Select your institution" dropdown before the
       username/password form, add a step here to click the right option.

    3. If the IdP shows a multi-factor challenge after the password, you
       will need to add another wait/interaction step here.
    ───────────────────────────────────────────────────────────────────────
    """
    username = os.environ.get("STUDIP_USERNAME")
    password = os.environ.get("STUDIP_PASSWORD")

    if not username or not password:
        log.error("STUDIP_USERNAME and/or STUDIP_PASSWORD not set in .env")
        sys.exit(1)

    log.info("Navigating to login page…")
    await page.goto(LOGIN_URL, wait_until="networkidle")

    # If already logged in, Stud.IP redirects away from index.php immediately.
    if "index.php" not in page.url:
        log.info("Already authenticated (redirected to %s).", page.url)
        await page.goto(MY_COURSES_URL, wait_until="networkidle")
        return

    log.info("Filling login credentials…")
    try:
        await page.wait_for_selector(SSO_USERNAME_SELECTOR, timeout=SSO_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        log.error(
            "Username field not found. Current URL: %s — check SSO_USERNAME_SELECTOR.", page.url
        )
        raise

    await page.fill(SSO_USERNAME_SELECTOR, username)
    await page.fill(SSO_PASSWORD_SELECTOR, password)
    await page.click(SSO_SUBMIT_SELECTOR)

    # Wait until we leave the login page.
    try:
        await page.wait_for_url(
            lambda u: "index.php" not in u,
            timeout=SSO_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        log.error(
            "Still on login page after submitting credentials — wrong username/password "
            "or the form selector is wrong. Current URL: %s", page.url
        )
        raise

    await page.goto(MY_COURSES_URL, wait_until="networkidle")
    log.info("Login successful. Current URL: %s", page.url)


# ---------------------------------------------------------------------------
# Semester course discovery
# ---------------------------------------------------------------------------

async def _scrape_courses_from_page(page: Page, seen_urls: set) -> list[dict]:
    """Collect all course links from the currently loaded my_courses page."""
    SKIP_URL   = re.compile(r'wizard|logout|login|profile/|messages|calendar|settings|globalsearch|jsupdater|my_institutes|my_courses/store|my_courses/groups|tabularasa|mark_notification', re.IGNORECASE)
    COURSE_URL = re.compile(r'seminar_main\.php\?auswahl=|auswahl=[a-f0-9]{10}', re.IGNORECASE)

    pairs = await page.evaluate("""() =>
        [...document.querySelectorAll('a[href]')].map(a => ({
            href: a.href,
            name: a.textContent.trim()
        }))
    """)
    log.info("  Scanning %s — %d anchors", page.url, len(pairs))

    courses = []
    for pair in pairs:
        href = (pair.get('href') or '').strip()
        name = (pair.get('name') or '').strip()
        if not href or not name:
            continue
        if SKIP_URL.search(href):
            continue
        if not COURSE_URL.search(href):
            continue
        norm = re.sub(r'&redirect_to=[^&]*', '', href)
        if norm in seen_urls:
            continue
        seen_urls.add(norm)
        courses.append({"name": name, "url": norm})
    return courses


async def get_all_semester_courses(page: Page) -> list[dict]:
    """
    Return courses grouped by semester by switching the semester filter for
    each recent semester and scraping the resulting my_courses page.
    """
    SET_SEM_URL = f"{STUDIP_BASE}/dispatch.php/my_courses/set_semester"
    SEM_RE      = re.compile(r'(?:SoSe|WiSe)\s+\d{4}', re.IGNORECASE)

    # ── Step 1: load the page and extract semester options from the filter ──
    await page.goto(MY_COURSES_URL, wait_until="networkidle")
    await page.wait_for_timeout(500)

    semester_options = await page.evaluate("""() => {
        const sel = document.querySelector('select[name="sem_select"]');
        if (!sel) return [];
        return [...sel.options]
            .filter(o => /SoSe|WiSe/i.test(o.title || o.textContent))
            .map(o => ({ id: o.value, name: (o.title || o.textContent).trim() }));
    }""")

    if not semester_options:
        log.warning("Could not find semester filter — falling back to single-page scrape.")
        seen: set[str] = set()
        courses = await _scrape_courses_from_page(page, seen)
        if not courses:
            debug_path = Path(__file__).parent / "debug_page.html"
            try:
                debug_path.write_text(await page.content(), encoding="utf-8")
                log.warning("No courses found — page HTML saved to %s", debug_path)
            except Exception:
                pass
            log.warning("No courses found — try --no-headless to inspect the page.")
            return []
        log.info("Found %d course(s) total (no semester grouping)", len(courses))
        return [{"semester": "Alle Kurse", "courses": courses}]

    log.info("Found %d semester(s) in filter: %s",
             len(semester_options), [s["name"] for s in semester_options[:5]])

    # ── Step 2: for each semester, set filter → scrape ──────────────────────
    # Only scrape recent semesters (skip far future/past beyond last 6)
    MAX_SEMESTERS = 6
    semesters_to_scrape = semester_options[:MAX_SEMESTERS]

    results: list[dict] = []
    seen_urls: set[str] = set()

    for sem in semesters_to_scrape:
        sem_name = sem["name"]
        sem_id   = sem["id"]

        # Set the semester filter (GET request changes session state)
        await page.goto(f"{SET_SEM_URL}?sem_select={sem_id}", wait_until="networkidle")
        await page.wait_for_timeout(300)
        await page.goto(MY_COURSES_URL, wait_until="networkidle")
        await page.wait_for_timeout(500)

        log.info("── Semester: %s", sem_name)
        courses = await _scrape_courses_from_page(page, seen_urls)
        if courses:
            log.info("  → %d course(s)", len(courses))
            results.append({"semester": sem_name, "courses": courses})
        else:
            log.info("  → no courses")

    total = sum(len(s["courses"]) for s in results)
    if not total:
        log.warning("No courses found in any semester.")
        return []

    log.info("Found %d course(s) across %d semester(s)", total, len(results))
    return results


# ---------------------------------------------------------------------------
# File downloads for a single course
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"Alle Kurse"}  # fallback dirs the scraper itself creates — skip in search

def _find_existing_course_dir(courses_root: Path, course_name: str) -> Path | None:
    """
    Search for an existing directory named course_name one level deep under courses_root.
    Semester folders (SoSe / WiSe / SS / WS) are preferred over generic catch-all folders.
    Skips directories in _SKIP_DIRS so the scraper's own fallback folder is never
    returned as an 'existing' location.
    """
    # Direct child (not under a semester group)
    direct = courses_root / course_name
    if direct.is_dir():
        return direct

    # Collect matches, then sort so semester-named parents come first
    matches: list[Path] = []
    try:
        for top_dir in courses_root.iterdir():
            if not top_dir.is_dir() or top_dir.name.startswith('.'):
                continue
            if top_dir.name in _SKIP_DIRS:
                continue
            candidate = top_dir / course_name
            if candidate.is_dir():
                matches.append(candidate)
    except PermissionError:
        pass

    if not matches:
        return None

    # Prefer semester-named parents (SoSe / WiSe / SS / WS)
    sem_re = re.compile(r'(?:SoSe|WiSe|SS|WS)', re.IGNORECASE)
    sem_matches = [m for m in matches if sem_re.search(m.parent.name)]
    return (sem_matches or matches)[0]


async def download_course_files(page: Page, course: dict, output_root: Path,
                                courses_root: Path | None = None) -> None:
    """
    Download all files for a course using the Stud.IP REST API.
    The API uses the same session cookies as the browser.

    courses_root: top-level directory to search for an existing course folder
                  (e.g. COURSES_DIR). If provided and an existing folder is found
                  it takes precedence over output_root / course_name.
    """
    course_name = sanitize_dirname(course["name"])

    # Prefer an already-existing course directory so new folders land in the
    # right place (e.g. SoSe 2026/Neurodynamics rather than Alle Kurse/Neurodynamics).
    dest_dir = None
    if courses_root and courses_root != output_root:
        dest_dir = _find_existing_course_dir(courses_root, course_name)
        if dest_dir:
            log.info("  Existing dir: %s", dest_dir)
    if dest_dir is None:
        dest_dir = output_root / course_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    log.info("── Course: %s", course["name"])

    # Extract course ID (cid or auswahl parameter)
    match = re.search(r"(?:auswahl|cid)=([a-f0-9]+)", course["url"])
    if not match:
        log.warning("  Could not extract course ID from URL: %s", course["url"])
        return
    cid = match.group(1)
    log.info("  cid: %s", cid)

    # Use the browser context's request object so API calls share the session cookies
    api = page.context.request

    # Fetch and store course metadata (non-blocking — failure is silently ignored)
    meta = await _fetch_course_meta(api, cid)
    if meta:
        course["meta"] = meta
        log.info("  Instructor(s): %s", ", ".join(meta.get("lecturers", [])) or "–")

    folder_id = await _get_top_folder_id(api, cid)
    if not folder_id:
        log.warning("  No files folder found for %s (cid=%s)", course["name"], cid)
        return

    await _api_download_folder(api, dest_dir, folder_id)


async def _api_download_folder(api, dest_dir: Path, folder_id: str, depth: int = 0) -> None:
    """
    Recursively download files using the Stud.IP REST API.
    GET /api.php/folder/<folder_id> returns subfolders and file_refs.
    GET /api.php/file/<file_id>/download streams the file.
    """
    if depth > 10:
        log.warning("Max folder depth reached.")
        return

    indent = "  " * depth
    resp = await api.get(f"{STUDIP_BASE}/api.php/folder/{folder_id}")
    if resp.status != 200:
        log.warning("%sCould not fetch folder %s (HTTP %d)", indent, folder_id, resp.status)
        return

    data = await resp.json()

    # --- Sub-folders ---
    for sub in data.get("subfolders", []):
        sub_id   = sub.get("id") or sub.get("folder_id", "")
        sub_name = sanitize_dirname(sub.get("name", "")) or "folder"
        if not sub_id:
            continue
        sub_dir = dest_dir / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        log.info("%s→ Folder: %s", indent, sub_name)
        await _api_download_folder(api, sub_dir, sub_id, depth + 1)

    # --- Files ---
    for file_ref in data.get("file_refs", []):
        file_id  = file_ref.get("id") or file_ref.get("file_id", "")
        filename = sanitize_dirname(file_ref.get("name", "")) or file_id
        if not file_id:
            continue

        if already_exists(dest_dir, filename):
            log.info("%s✓ Already exists: %s", indent, filename)
            continue

        log.info("%s↓ %s", indent, filename)
        try:
            dl_resp = await api.get(f"{STUDIP_BASE}/api.php/file/{file_id}/download")
            if dl_resp.status != 200:
                log.warning("%s  HTTP %d for %s", indent, dl_resp.status, filename)
                continue
            body = await dl_resp.body()
            save_path = dest_dir / filename
            save_path.write_bytes(body)
            log.info("%s  Saved: %s", indent, save_path)
        except Exception as exc:
            log.warning("%s  Failed: %s — %s", indent, filename, exc)


class _TextExtractor(HTMLParser):
    """Strip all HTML tags and return plain text."""
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r'\s+', ' ', ''.join(self._parts)).strip()


async def _fetch_details_page_description(api, cid: str) -> str:
    """
    Scrape the description from the Stud.IP course details HTML page.
    Returns empty string if nothing useful found.
    """
    url = f"{STUDIP_BASE}/dispatch.php/course/details/?cid={cid}"
    try:
        resp = await api.get(url)
        if resp.status != 200:
            return ""
        html = await resp.text()
    except Exception:
        return ""

    # Strategy 1: look for a <section> whose header contains "Beschreibung"
    # or a <div class="..."> following such a heading.
    # Stud.IP 4/5 wraps content blocks like:
    #   <section class="content-block"><header><h1>Beschreibung</h1></header>
    #     <div class="content">...</div></section>
    m = re.search(
        r'Beschreibung\b.{0,300}?<(?:div|td)[^>]*class="[^"]*(?:content|desc|text)[^"]*"[^>]*>(.*?)</(?:div|td)>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        ex = _TextExtractor()
        ex.feed(m.group(1))
        desc = ex.text()
        if len(desc) > 10:
            return desc

    # Strategy 2: table row – label cell says "Beschreibung", next cell has value
    m = re.search(
        r'<td[^>]*>[\s\S]{0,50}?Beschreibung[\s\S]{0,50}?</td>\s*<td[^>]*>([\s\S]*?)</td>',
        html, re.IGNORECASE,
    )
    if m:
        ex = _TextExtractor()
        ex.feed(m.group(1))
        desc = ex.text()
        if len(desc) > 10:
            return desc

    # Strategy 3: any <div> with id or class containing "description"
    m = re.search(
        r'<div[^>]+(?:id|class)="[^"]*description[^"]*"[^>]*>([\s\S]*?)</div>',
        html, re.IGNORECASE,
    )
    if m:
        ex = _TextExtractor()
        ex.feed(m.group(1))
        desc = ex.text()
        if len(desc) > 10:
            return desc

    return ""


async def _fetch_course_meta(api, cid: str) -> dict:
    """
    Fetch course metadata (title, description, instructors, type, …) from the StudIP REST API.
    Returns a cleaned dict suitable for storage in courses.json.
    """
    resp = await api.get(f"{STUDIP_BASE}/api.php/course/{cid}")
    if resp.status != 200:
        return {}
    try:
        data = await resp.json()
    except Exception:
        return {}

    # Lecturers: dict of {user_id: {name: {formatted: ...}, ...}}
    lecturers_raw = data.get("members", {}) or data.get("lecturers", {}) or {}
    # The API may return a flat list or a nested dict depending on the endpoint version
    lecturers: list[str] = []
    def _extract_name(v):
        if not isinstance(v, dict):
            return ""
        raw = v.get("name")
        if isinstance(raw, dict):
            return raw.get("formatted") or ""
        if isinstance(raw, str):
            return raw
        return v.get("fullname") or ""

    if isinstance(lecturers_raw, dict):
        for v in lecturers_raw.values():
            name = _extract_name(v)
            if name:
                lecturers.append(name)
    elif isinstance(lecturers_raw, list):
        for v in lecturers_raw:
            name = _extract_name(v)
            if name:
                lecturers.append(name)

    # Try dedicated lecturers endpoint if the main one returned nothing
    if not lecturers:
        r2 = await api.get(f"{STUDIP_BASE}/api.php/course/{cid}/members")
        if r2.status == 200:
            try:
                members = await r2.json()
                for role_key in ("dozenten", "lecturers", "teachers"):
                    role_data = members.get(role_key) if isinstance(members, dict) else None
                    if role_data:
                        for v in (role_data.values() if isinstance(role_data, dict) else role_data):
                            name = (v.get("name") or {}).get("formatted") or v.get("fullname") or ""
                            if name:
                                lecturers.append(name)
                        break
            except Exception:
                pass

    course_type = data.get("type", "") or data.get("form", "")
    type_labels = {
        "1": "Vorlesung", "2": "Seminar", "3": "Übung", "4": "Praktikum",
        "5": "Kolloquium", "6": "AG", "99": "Sonstiges",
        "Vorlesung": "Vorlesung", "Seminar": "Seminar",
    }
    type_label = type_labels.get(str(course_type), str(course_type)) if course_type else ""

    # REST API often returns empty description — fall back to HTML details page
    description = (data.get("description") or "").strip()
    if not description:
        description = await _fetch_details_page_description(api, cid)

    return {
        "title":       data.get("title") or data.get("name") or "",
        "subtitle":    data.get("subtitle") or "",
        "description": description,
        "type":        type_label,
        "lecturers":   lecturers,
        "semester":    data.get("start_semester", {}).get("title", "") if isinstance(data.get("start_semester"), dict) else "",
        "location":    data.get("location") or "",
        "ects":        data.get("ects") or "",
        "participants": data.get("admission_turnout") or "",
    }


async def _get_top_folder_id(api, cid: str) -> str | None:
    """Fetch the root folder ID for a course via the Stud.IP API."""
    resp = await api.get(f"{STUDIP_BASE}/api.php/course/{cid}/top_folder")
    if resp.status != 200:
        log.warning("  top_folder HTTP %d for cid=%s — trying /folders fallback", resp.status, cid)
        # Fallback: list all folders for the course
        resp2 = await api.get(f"{STUDIP_BASE}/api.php/course/{cid}/folders")
        if resp2.status == 200:
            data2 = await resp2.json()
            folders = data2 if isinstance(data2, list) else data2.get("collection", [])
            if folders:
                fid = folders[0].get("id") or folders[0].get("folder_id")
                log.info("  Fallback folder id: %s", fid)
                return fid
        log.warning("  No folder found for cid=%s", cid)
        return None
    data = await resp.json()
    fid = data.get("id") or data.get("folder_id")
    if not fid:
        log.warning("  top_folder response has no id field: %s", str(data)[:200])
    return fid


# ---------------------------------------------------------------------------
# Course registry (persists course URLs for per-course re-sync)
# ---------------------------------------------------------------------------

def load_course_registry() -> dict:
    if COURSES_JSON.exists():
        return json.loads(COURSES_JSON.read_text(encoding="utf-8"))
    return {}


def save_course_registry(registry: dict) -> None:
    COURSES_JSON.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stud.IP file scraper for the University of Osnabrück",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        metavar="COURSE_URL",
        help="Scrape a single course URL instead of the current semester overview.",
    )
    parser.add_argument(
        "--course",
        metavar="COURSE_PATH",
        help="Re-sync a course by its local relative path (e.g. 'Alle Kurse/Neurodynamics'). "
             "Looks up the URL from courses.json.",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        default=os.environ.get("COURSES_DIR", "/Users/maxmacbookpro/Documents/Uni/Cognitive Science [Course]/Courses"),
        help="Root directory for downloaded files (default: $COURSES_DIR env var).",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser in headless mode (default: headless). "
             "Use --no-headless to watch the browser.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    log.info("Output directory: %s", output_root)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await login(page)

            if args.course:
                # ── Mode C: re-sync by local path ─────────────────────────
                log.info("Mode C — re-sync by path: %s", args.course)
                registry = load_course_registry()
                entry = registry.get(args.course)
                if not entry:
                    log.error("Course path '%s' not found in courses.json. "
                              "Run a full scrape first.", args.course)
                    return
                course_url  = entry["url"]
                course_root = output_root / Path(args.course).parent
                course_root.mkdir(parents=True, exist_ok=True)
                await download_course_files(
                    page,
                    {"name": entry["name"], "url": course_url},
                    course_root,
                    courses_root=output_root,
                )

            elif args.url:
                # ── Mode B: single course by URL ──────────────────────────
                log.info("Mode B — targeted course: %s", args.url)
                await page.goto(args.url, wait_until="networkidle")
                title = await page.title()
                course_name = title.split("–")[0].split("-")[0].strip() or "course"
                await download_course_files(
                    page,
                    {"name": course_name, "url": args.url},
                    output_root,
                )

            else:
                # ── Mode A: all semesters ─────────────────────────────────
                log.info("Mode A — scraping all semesters…")
                semesters = await get_all_semester_courses(page)

                if not semesters:
                    log.warning(
                        "No courses found. The CSS selectors for the course list "
                        "may need updating — run with --no-headless to inspect the page."
                    )
                    return

                registry = load_course_registry()
                for sem in semesters:
                    semester_dir = output_root / sanitize_dirname(sem["semester"])
                    semester_dir.mkdir(parents=True, exist_ok=True)
                    log.info("── Semester: %s", sem["semester"])
                    for course in sem["courses"]:
                        course_name = sanitize_dirname(course["name"])
                        rel_path = f"{sanitize_dirname(sem['semester'])}/{course_name}"
                        registry[rel_path] = {
                            "name": course["name"],
                            "url":  course["url"],
                            "meta": course.get("meta", registry.get(rel_path, {}).get("meta", {})),
                        }
                        await download_course_files(page, course, semester_dir,
                                                    courses_root=output_root)
                save_course_registry(registry)
                log.info("Saved %d course URLs to courses.json", len(registry))

        finally:
            await browser.close()

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
