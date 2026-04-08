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
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

STUDIP_BASE = "https://studip.uni-osnabrueck.de"
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

async def get_all_semester_courses(page: Page) -> list[dict]:
    """
    Parse the my_courses overview and return courses grouped by semester.
    Uses Playwright's native locator API (more reliable than page.evaluate
    for pages that render links inside iframes or shadow DOM).
    """
    # MY_COURSES_ALL_URL (?sem_select=all) acts as a redirect/settings call
    # and returns an empty shell page without the course list.
    # Scrape the regular my_courses page + the archive page instead.
    pages_to_scrape = [
        MY_COURSES_URL,
        f"{STUDIP_BASE}/dispatch.php/my_courses/archive",
    ]

    SKIP_URL   = re.compile(r'wizard|logout|login|profile/|messages|calendar|settings|globalsearch|jsupdater|my_institutes|my_courses/store|my_courses/groups|tabularasa|mark_notification', re.IGNORECASE)
    COURSE_URL = re.compile(r'seminar_main\.php\?auswahl=|auswahl=[a-f0-9]{10}', re.IGNORECASE)

    course_links: list[dict] = []
    seen_urls: set[str] = set()

    for url in pages_to_scrape:
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(500)

        # Collect all href+text pairs in a single JS call (much faster than
        # iterating element handles one by one via the Playwright IPC bridge).
        pairs = await page.evaluate("""() =>
            [...document.querySelectorAll('a[href]')].map(a => ({
                href: a.href,
                name: a.textContent.trim()
            }))
        """)
        log.info("Scanning %s — %d anchors", url, len(pairs))

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
            course_links.append({"name": name, "url": norm})

    if not course_links:
        debug_path = Path(__file__).parent / "debug_page.html"
        try:
            await page.goto(MY_COURSES_URL, wait_until="networkidle")
            debug_path.write_text(await page.content(), encoding="utf-8")
            log.warning("No courses found — page HTML saved to %s", debug_path)
        except Exception:
            pass
        log.warning("No courses found — try --no-headless to inspect the page.")
        return []

    log.info("Found %d course(s) total", len(course_links))
    return [{"semester": "Alle Kurse", "courses": course_links}]


# ---------------------------------------------------------------------------
# File downloads for a single course
# ---------------------------------------------------------------------------

async def download_course_files(page: Page, course: dict, output_root: Path) -> None:
    """
    Download all files for a course using the Stud.IP REST API.
    The API uses the same session cookies as the browser.
    """
    course_name = sanitize_dirname(course["name"])
    dest_dir = output_root / course_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    log.info("── Course: %s", course["name"])

    # Extract course ID (cid or auswahl parameter)
    match = re.search(r"(?:auswahl|cid)=([a-f0-9]+)", course["url"])
    if not match:
        log.warning("  Could not extract course ID from URL: %s", course["url"])
        return
    cid = match.group(1)

    # Use the browser context's request object so API calls share the session cookies
    api = page.context.request

    folder_id = await _get_top_folder_id(api, cid)
    if not folder_id:
        log.info("  No files folder found (course may have no Dateien tab).")
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


async def _get_top_folder_id(api, cid: str) -> str | None:
    """Fetch the root folder ID for a course via the Stud.IP API."""
    resp = await api.get(f"{STUDIP_BASE}/api.php/course/{cid}/top_folder")
    if resp.status != 200:
        log.warning("Could not fetch top_folder for course %s (HTTP %d)", cid, resp.status)
        return None
    data = await resp.json()
    return data.get("id") or data.get("folder_id")


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

            if args.url:
                # ── Mode B: single course ──────────────────────────────────
                log.info("Mode B — targeted course: %s", args.url)
                # Derive a display name from the URL or page title.
                await page.goto(args.url, wait_until="networkidle")
                title = await page.title()
                course_name = title.split("–")[0].split("-")[0].strip() or "course"
                await download_course_files(
                    page,
                    {"name": course_name, "url": args.url},
                    output_root,
                )
            else:
                # ── Mode A: all semesters ──────────────────────────────────
                log.info("Mode A — scraping all semesters…")
                semesters = await get_all_semester_courses(page)

                if not semesters:
                    log.warning(
                        "No courses found. The CSS selectors for the course list "
                        "may need updating — run with --no-headless to inspect the page."
                    )
                    return

                for sem in semesters:
                    semester_dir = output_root / sanitize_dirname(sem["semester"])
                    semester_dir.mkdir(parents=True, exist_ok=True)
                    log.info("── Semester: %s", sem["semester"])
                    for course in sem["courses"]:
                        await download_course_files(page, course, semester_dir)

        finally:
            await browser.close()

    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
