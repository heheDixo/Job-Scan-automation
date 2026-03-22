"""
apply_job.py — Auto-apply to a job using Playwright stealth browser automation.

Features:
- playwright-stealth to avoid bot fingerprinting
- Randomized human-like delays (1-4s between actions)
- CAPTCHA detection → falls back gracefully (caller handles fallback)
- Takes success screenshot

Usage:
    python tools/apply_job.py --url "https://..." --cover-letter "..." [--headless]

Returns JSON to stdout:
    {"success": true/false, "method": "auto"/"captcha_detected"/"error", "message": "..."}
"""

import argparse
import json
import os
import random
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, ".tmp", "screenshots")

CAPTCHA_SIGNALS = [
    "captcha", "recaptcha", "hcaptcha", "cf-challenge",
    "i am not a robot", "verify you are human", "cloudflare",
    "just a moment", "security check",
]

COMMON_FORM_FIELDS = {
    "name":         ["name", "full_name", "fullname", "applicant_name"],
    "email":        ["email", "email_address", "e-mail"],
    "phone":        ["phone", "phone_number", "mobile", "telephone"],
    "linkedin":     ["linkedin", "linkedin_url", "linkedin_profile"],
    "cover_letter": ["cover_letter", "coverletter", "message", "cover letter"],
    "resume_text":  ["resume", "cv", "resume_text", "experience"],
}


def _random_delay(min_s: float = 1.0, max_s: float = 3.5):
    time.sleep(random.uniform(min_s, max_s))


def _is_captcha_page(page) -> bool:
    html = page.content().lower()
    return any(signal in html for signal in CAPTCHA_SIGNALS)


def _find_input(page, field_names: list):
    """Try to find a form input by common name/id/placeholder patterns."""
    for name in field_names:
        for selector in [
            f'input[name*="{name}" i]',
            f'input[id*="{name}" i]',
            f'input[placeholder*="{name}" i]',
            f'textarea[name*="{name}" i]',
            f'textarea[id*="{name}" i]',
            f'textarea[placeholder*="{name}" i]',
        ]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
    return None


def _load_resume_data() -> dict:
    """Parse key fields from my_resume.txt for form filling."""
    resume_path = os.path.join(BASE_DIR, "my_resume.txt")
    if not os.path.exists(resume_path):
        return {}
    with open(resume_path) as f:
        text = f.read()
    # Extract email
    import re
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]+", text)
    phone_match = re.search(r"(\+?[\d\s\-().]{10,})", text)
    linkedin_match = re.search(r"linkedin\.com/in/[\w-]+", text, re.IGNORECASE)
    name_match = re.match(r"^([A-Z][a-z]+(?: [A-Z][a-z]+)+)", text.strip())

    return {
        "name": name_match.group(1) if name_match else "",
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(1).strip() if phone_match else "",
        "linkedin": f"https://{linkedin_match.group(0)}" if linkedin_match else "",
        "resume_text": text[:3000],
    }


def apply(url: str, cover_letter: str, headless: bool = True) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth_sync
    except ImportError as e:
        return {"success": False, "method": "error",
                "message": f"Missing dependency: {e}. Run: pip install playwright playwright-stealth && playwright install chromium"}

    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    resume_data = _load_resume_data()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            _random_delay(2, 4)

            # Check for CAPTCHA immediately after page load
            if _is_captcha_page(page):
                browser.close()
                return {
                    "success": False,
                    "method": "captcha_detected",
                    "message": "CAPTCHA detected on the page. Please apply manually.",
                    "url": url,
                }

            # Try to fill form fields
            filled_any = False

            if resume_data.get("name"):
                el = _find_input(page, COMMON_FORM_FIELDS["name"])
                if el:
                    el.fill(resume_data["name"])
                    _random_delay(0.5, 1.5)
                    filled_any = True

            if resume_data.get("email"):
                el = _find_input(page, COMMON_FORM_FIELDS["email"])
                if el:
                    el.fill(resume_data["email"])
                    _random_delay(0.5, 1.5)
                    filled_any = True

            if resume_data.get("phone"):
                el = _find_input(page, COMMON_FORM_FIELDS["phone"])
                if el:
                    el.fill(resume_data["phone"])
                    _random_delay(0.5, 1.5)

            if resume_data.get("linkedin"):
                el = _find_input(page, COMMON_FORM_FIELDS["linkedin"])
                if el:
                    el.fill(resume_data["linkedin"])
                    _random_delay(0.5, 1.5)

            if cover_letter:
                el = _find_input(page, COMMON_FORM_FIELDS["cover_letter"])
                if el:
                    el.fill(cover_letter)
                    _random_delay(1, 2)
                    filled_any = True

            # Check for CAPTCHA again after interacting
            if _is_captcha_page(page):
                browser.close()
                return {
                    "success": False,
                    "method": "captcha_detected",
                    "message": "CAPTCHA appeared during form fill. Please apply manually.",
                    "url": url,
                }

            if not filled_any:
                # Page may have a redirect or iframe-based form; can't auto-fill
                screenshot_path = os.path.join(SCREENSHOTS_DIR, "unfillable_form.png")
                page.screenshot(path=screenshot_path)
                browser.close()
                return {
                    "success": False,
                    "method": "unfillable",
                    "message": "Could not find standard form fields. Please apply manually.",
                    "url": url,
                    "screenshot": screenshot_path,
                }

            # Take screenshot BEFORE submitting (for review)
            job_id = url.split("/")[-1][:12].replace("?", "")
            screenshot_path = os.path.join(SCREENSHOTS_DIR, f"filled_{job_id}.png")
            page.screenshot(path=screenshot_path)

            browser.close()
            return {
                "success": True,
                "method": "auto",
                "message": "Form filled successfully. Screenshot saved.",
                "screenshot": screenshot_path,
            }

        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            return {
                "success": False,
                "method": "error",
                "message": str(e),
            }


def main():
    parser = argparse.ArgumentParser(description="Auto-apply to a job posting")
    parser.add_argument("--url", required=True, help="Job application URL")
    parser.add_argument("--cover-letter", default="", help="Cover letter text")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default: True)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show browser window (useful for debugging)")
    args = parser.parse_args()

    result = apply(args.url, args.cover_letter, args.headless)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
