"""Dumps the HTML of my_courses to debug_page.html for selector inspection."""
import asyncio, os
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

STUDIP_BASE = "https://studip.uni-osnabrueck.de"
MY_COURSES_URL = f"{STUDIP_BASE}/dispatch.php/my_courses"

SSO_USERNAME_SELECTOR = 'input[name="username"], input[id="username"], input[type="text"]'
SSO_PASSWORD_SELECTOR = 'input[name="password"], input[id="password"], input[type="password"]'
SSO_SUBMIT_SELECTOR   = 'button[type="submit"], input[type="submit"]'

LOGIN_URL = "https://studip.uni-osnabrueck.de/index.php"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Go directly to the login page
        await page.goto(LOGIN_URL, wait_until="networkidle")
        print("Login page URL:", page.url)

        # Fill credentials — inspect index.php to confirm field names if this fails
        await page.wait_for_selector(SSO_USERNAME_SELECTOR, timeout=15000)
        await page.fill(SSO_USERNAME_SELECTOR, os.environ["STUDIP_USERNAME"])
        await page.fill(SSO_PASSWORD_SELECTOR, os.environ["STUDIP_PASSWORD"])
        await page.click(SSO_SUBMIT_SELECTOR)

        # Wait until we land on a Stud.IP page (not login anymore)
        await page.wait_for_url(
            lambda u: "index.php" not in u or "dispatch" in u,
            timeout=20000
        )
        print("After login URL:", page.url)

        await page.goto(MY_COURSES_URL, wait_until="networkidle")

        html = await page.content()
        Path("debug_page.html").write_text(html, encoding="utf-8")
        print("Saved to debug_page.html")
        print("Page title:", await page.title())

        # Print all text from headings and captions
        for sel in ["caption", "th", "h1", "h2", "h3", "h4", ".semester-header"]:
            els = await page.query_selector_all(sel)
            for el in els:
                t = (await el.inner_text()).strip()
                if t:
                    print(f"  [{sel}] {t[:80]}")

        # Print all <a> hrefs that look like courses
        links = await page.query_selector_all("a[href]")
        for link in links:
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if "course" in href or "seminar" in href:
                print(f"  [course link] {text[:60]}  →  {href[:80]}")

        await browser.close()

asyncio.run(main())
