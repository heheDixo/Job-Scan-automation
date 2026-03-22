"""
apply_job.py — Auto-apply to a job using Playwright stealth browser automation.

Features:
- playwright-stealth to avoid bot fingerprinting
- Randomized human-like delays (1-4s between actions)
- CAPTCHA detection → falls back gracefully (caller handles fallback)
- Takes pre-fill and post-submit screenshots

Usage:
    python tools/apply_job.py --url "https://..." --cover-letter-file /tmp/cl.txt [--headless]
    python tools/apply_job.py --url "https://..." --cover-letter "text..." [--headless]

Returns JSON to stdout:
    {"success": true/false, "method": "auto"/"captcha_detected"/"error", "message": "..."}
"""

import argparse
import json
import os
import random
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LINKEDIN_COOKIES_PATH = os.path.join(BASE_DIR, ".tmp", "linkedin_cookies.json")
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

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Apply")',
    'button:has-text("Submit")',
    'button:has-text("Send Application")',
    'button:has-text("Submit Application")',
    'a:has-text("Apply Now")',
    'button:has-text("Apply Now")',
]


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


def _is_linkedin_url(url: str) -> bool:
    return "linkedin.com/jobs/view/" in url


def apply_linkedin(url: str, cover_letter: str, resume_data: dict, headless: bool = True) -> dict:
    """
    Apply via LinkedIn Easy Apply modal.
    Reuses the session cookies saved by search_jobs.py.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth_sync
    except ImportError as e:
        return {"success": False, "method": "error", "message": f"Missing dependency: {e}"}

    if not os.path.exists(LINKEDIN_COOKIES_PATH):
        return {
            "success": False, "method": "error",
            "message": "LinkedIn session not found. Run search_jobs.py first to log in.",
        }

    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    job_id_match = re.search(r"/jobs/view/(\d+)", url)
    job_id_str = job_id_match.group(1) if job_id_match else "unknown"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        try:
            with open(LINKEDIN_COOKIES_PATH) as f:
                context.add_cookies(json.load(f))
        except Exception as e:
            browser.close()
            return {"success": False, "method": "error", "message": f"Could not load LinkedIn cookies: {e}"}

        page = context.new_page()
        stealth_sync(page)

        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            _random_delay(2, 4)

            if "login" in page.url or "authwall" in page.url:
                browser.close()
                return {
                    "success": False, "method": "error",
                    "message": "LinkedIn session expired. Run search_jobs.py again to refresh login.",
                }

            if _is_captcha_page(page):
                browser.close()
                return {"success": False, "method": "captcha_detected", "message": "CAPTCHA detected.", "url": url}

            # Click Easy Apply button
            easy_apply_btn = (
                page.query_selector("button.jobs-apply-button") or
                page.query_selector("button[aria-label*='Easy Apply']") or
                page.query_selector("button:has-text('Easy Apply')")
            )
            if not easy_apply_btn or not easy_apply_btn.is_visible():
                browser.close()
                return {
                    "success": False, "method": "no_easy_apply",
                    "message": "No Easy Apply button found — job may require external application.",
                    "url": url,
                }

            easy_apply_btn.click()
            _random_delay(2, 3)

            # Walk through modal steps (max 8 steps to handle multi-page forms)
            for step in range(8):
                # Fill phone if empty
                phone_field = (
                    page.query_selector("input[id*='phoneNumber']") or
                    page.query_selector("input[name*='phone']") or
                    page.query_selector("input[aria-label*='Phone' i]")
                )
                if phone_field and phone_field.is_visible():
                    try:
                        if not phone_field.input_value() and resume_data.get("phone"):
                            phone_field.fill(resume_data["phone"])
                            _random_delay(0.4, 0.8)
                    except Exception:
                        pass

                # Fill cover letter textarea if present and empty
                cl_field = (
                    page.query_selector("textarea[id*='cover-letter']") or
                    page.query_selector("textarea[name*='coverLetter']") or
                    page.query_selector("textarea[aria-label*='cover letter' i]") or
                    page.query_selector("textarea[aria-label*='Cover Letter' i]")
                )
                if cl_field and cl_field.is_visible() and cover_letter:
                    try:
                        if not cl_field.input_value():
                            cl_field.fill(cover_letter[:2000])
                            _random_delay(0.5, 1)
                    except Exception:
                        pass

                # Auto-answer simple dropdowns (select first non-placeholder option)
                for select_el in page.query_selector_all("select"):
                    try:
                        if not select_el.is_visible():
                            continue
                        opts = select_el.query_selector_all("option")
                        if len(opts) > 1:
                            val = opts[1].get_attribute("value")
                            if val and not select_el.input_value():
                                select_el.select_option(val)
                    except Exception:
                        pass

                # Screenshot current step
                page.screenshot(path=os.path.join(SCREENSHOTS_DIR, f"li_step{step}_{job_id_str}.png"))

                # Submit
                submit_btn = (
                    page.query_selector("button[aria-label='Submit application']") or
                    page.query_selector("button:has-text('Submit application')")
                )
                if submit_btn and submit_btn.is_visible():
                    _random_delay(1, 2)
                    submit_btn.click()
                    _random_delay(2, 3)
                    final_ss = os.path.join(SCREENSHOTS_DIR, f"li_submitted_{job_id_str}.png")
                    page.screenshot(path=final_ss)
                    browser.close()
                    return {
                        "success": True,
                        "method": "linkedin_easy_apply",
                        "message": "Applied via LinkedIn Easy Apply.",
                        "screenshot": final_ss,
                    }

                # Review step
                review_btn = (
                    page.query_selector("button[aria-label='Review your application']") or
                    page.query_selector("button:has-text('Review')")
                )
                if review_btn and review_btn.is_visible():
                    review_btn.click()
                    _random_delay(1, 2)
                    continue

                # Next step
                next_btn = (
                    page.query_selector("button[aria-label='Continue to next step']") or
                    page.query_selector("button:has-text('Next')")
                )
                if next_btn and next_btn.is_visible():
                    next_btn.click()
                    _random_delay(1, 2)
                    continue

                # No navigation button found — stuck on a required field we can't fill
                browser.close()
                return {
                    "success": False, "method": "modal_incomplete",
                    "message": "Easy Apply modal has required fields that could not be auto-filled.",
                    "url": url,
                }

            browser.close()
            return {
                "success": False, "method": "modal_incomplete",
                "message": "Reached max steps in Easy Apply modal without submitting.",
                "url": url,
            }

        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            return {"success": False, "method": "error", "message": str(e)}


def apply(url: str, cover_letter: str, headless: bool = True) -> dict:
    resume_data = _load_resume_data()

    # Route LinkedIn jobs to the Easy Apply handler
    if _is_linkedin_url(url):
        return apply_linkedin(url, cover_letter, resume_data, headless)

    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth_sync
    except ImportError as e:
        return {"success": False, "method": "error",
                "message": f"Missing dependency: {e}. Run: pip install playwright playwright-stealth && playwright install chromium"}

    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

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

            # Find and click submit button
            submit_el = None
            for selector in SUBMIT_SELECTORS:
                try:
                    el = page.query_selector(selector)
                    if el and el.is_visible():
                        submit_el = el
                        break
                except Exception:
                    continue

            if not submit_el:
                browser.close()
                return {
                    "success": False,
                    "method": "no_submit_button",
                    "message": "Form filled but could not find a submit button. Please apply manually.",
                    "url": url,
                    "screenshot": screenshot_path,
                }

            _random_delay(1, 2)
            submit_el.click()
            _random_delay(2, 4)

            # Check for CAPTCHA post-submit
            if _is_captcha_page(page):
                browser.close()
                return {
                    "success": False,
                    "method": "captcha_detected",
                    "message": "CAPTCHA appeared after submit. Please apply manually.",
                    "url": url,
                }

            # Take post-submit screenshot as confirmation
            confirm_screenshot = os.path.join(SCREENSHOTS_DIR, f"submitted_{job_id}.png")
            page.screenshot(path=confirm_screenshot)

            browser.close()
            return {
                "success": True,
                "method": "auto",
                "message": "Form filled and submitted. Screenshot saved.",
                "screenshot": confirm_screenshot,
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
    parser.add_argument("--cover-letter", default="", help="Cover letter text (inline)")
    parser.add_argument("--cover-letter-file", default="", help="Path to file containing cover letter text")
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run browser in headless mode (default: True)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show browser window (useful for debugging)")
    args = parser.parse_args()

    cover_letter = args.cover_letter
    if args.cover_letter_file and os.path.exists(args.cover_letter_file):
        with open(args.cover_letter_file) as f:
            cover_letter = f.read()

    result = apply(args.url, cover_letter, args.headless)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
