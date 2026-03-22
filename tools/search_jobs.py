"""
search_jobs.py — Fetch remote jobs from LinkedIn (Easy Apply) and We Work Remotely.
Saves new jobs to SQLite. Skips duplicates, blacklisted entries, and internships.

Usage:
    python tools/search_jobs.py --query "AI engineer"
    python tools/search_jobs.py --query "ML engineer" --query "LLM engineer"
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time

import feedparser
import requests

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, save_job

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _extract_domain(url: str) -> str:
    match = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return match.group(1) if match else ""


def _matches_query(text: str, queries: list) -> bool:
    text_lower = text.lower()
    return any(q.lower() in text_lower for q in queries)


def _is_blacklisted(title: str, company: str, cfg: dict) -> bool:
    combined = (title + " " + company).lower()
    for company_bl in cfg.get("blacklisted_companies", []):
        if company_bl.lower() in combined:
            return True
    for kw in cfg.get("blacklisted_keywords", []):
        if kw.lower() in combined:
            return True
    return False


def _parse_salary(text: str) -> str:
    """Extract salary string from job description if present."""
    if not text:
        return ""
    match = re.search(
        r"(\$[\d,]+(?:k)?(?:\s*[-–]\s*\$[\d,]+(?:k)?)?(?:\s*/\s*(?:yr|year|mo|month|hr|hour))?)",
        text, re.IGNORECASE
    )
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# LinkedIn Easy Apply
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LINKEDIN_COOKIES_PATH = os.path.join(BASE_DIR, ".tmp", "linkedin_cookies.json")


def _linkedin_login(page, email: str, password: str) -> bool:
    """Log in to LinkedIn. Returns True on success."""
    page.goto("https://www.linkedin.com/login", timeout=30000, wait_until="domcontentloaded")
    time.sleep(random.uniform(1.5, 3))
    try:
        page.fill("#username", email)
        time.sleep(random.uniform(0.4, 0.9))
        page.fill("#password", password)
        time.sleep(random.uniform(0.4, 0.9))
        page.click('button[type="submit"]')
        time.sleep(random.uniform(3, 5))
    except Exception as e:
        print(f"[search] LinkedIn login interaction error: {e}")
        return False
    url = page.url
    return "feed" in url or "mynetwork" in url or ("/jobs" in url and "login" not in url)


def _save_linkedin_cookies(context):
    os.makedirs(os.path.dirname(LINKEDIN_COOKIES_PATH), exist_ok=True)
    with open(LINKEDIN_COOKIES_PATH, "w") as f:
        json.dump(context.cookies(), f)


def _load_linkedin_cookies(context) -> bool:
    if not os.path.exists(LINKEDIN_COOKIES_PATH):
        return False
    try:
        with open(LINKEDIN_COOKIES_PATH) as f:
            context.add_cookies(json.load(f))
        return True
    except Exception:
        return False


def fetch_linkedin(queries: list, cfg: dict) -> list:
    """
    Scrape LinkedIn Jobs with Easy Apply + Remote filters using Playwright.
    Job type filter excludes Internships (f_JT=F,C,P,T — no I).
    Requires LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import stealth_sync
    except ImportError:
        print("[search] LinkedIn: playwright not installed — skipping")
        return []

    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    email = os.getenv("LINKEDIN_EMAIL", "")
    password = os.getenv("LINKEDIN_PASSWORD", "")
    if not email or not password:
        print("[search] LinkedIn: LINKEDIN_EMAIL/LINKEDIN_PASSWORD not set in .env — skipping")
        return []

    print("[search] Fetching LinkedIn Jobs (Easy Apply, Remote, no internships)...")
    results = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        stealth_sync(page)

        # Try saved session first
        logged_in = False
        if _load_linkedin_cookies(context):
            page.goto("https://www.linkedin.com/jobs/", timeout=30000, wait_until="domcontentloaded")
            time.sleep(2)
            logged_in = "login" not in page.url and "authwall" not in page.url

        if not logged_in:
            logged_in = _linkedin_login(page, email, password)
            if logged_in:
                _save_linkedin_cookies(context)
            else:
                print("[search] LinkedIn: Login failed — check LINKEDIN_EMAIL/LINKEDIN_PASSWORD in .env")
                browser.close()
                return []

        for query in queries[:5]:  # Cap at 5 queries to avoid triggering bot detection
            encoded = query.replace(" ", "%20")
            # f_AL=true  → Easy Apply only
            # f_WT=2     → Remote
            # f_JT=F,C,P,T → Full-time/Contract/Part-time/Temporary (excludes Internship=I)
            search_url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={encoded}&f_AL=true&f_WT=2&f_JT=F%2CC%2CP%2CT&start=0"
            )
            try:
                page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(random.uniform(2.5, 4))

                # Scroll to trigger lazy loading
                for _ in range(2):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1.5)

                job_items = page.query_selector_all("li.jobs-search-results__list-item")
                if not job_items:
                    job_items = page.query_selector_all(".job-card-container")

                for card in job_items[:20]:
                    try:
                        title_el = (
                            card.query_selector("a.job-card-list__title") or
                            card.query_selector(".job-card-list__title") or
                            card.query_selector("strong")
                        )
                        company_el = (
                            card.query_selector(".job-card-container__primary-description") or
                            card.query_selector(".artdeco-entity-lockup__subtitle span")
                        )
                        link_el = card.query_selector("a[href*='/jobs/view/']")

                        if not title_el or not link_el:
                            continue

                        title = title_el.inner_text().strip()
                        company = company_el.inner_text().strip() if company_el else ""
                        href = link_el.get_attribute("href") or ""

                        id_match = re.search(r"/jobs/view/(\d+)", href)
                        if not id_match:
                            continue
                        linkedin_id = id_match.group(1)
                        if linkedin_id in seen_ids:
                            continue
                        seen_ids.add(linkedin_id)

                        job_url = f"https://www.linkedin.com/jobs/view/{linkedin_id}/"

                        if not title:
                            continue
                        if _is_blacklisted(title, company, cfg):
                            continue

                        results.append({
                            "id": _make_id(job_url),
                            "title": title,
                            "company": company,
                            "url": job_url,
                            "domain": "",
                            "description": "",
                            "tags": query,
                            "salary": "",
                            "source": "linkedin",
                        })
                    except Exception:
                        continue

                time.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"[search] LinkedIn error for '{query}': {e}")
                continue

        browser.close()

    print(f"[search] LinkedIn: {len(results)} Easy Apply jobs found")
    return results


# --------------------------------------------------------------------------- #
# RemoteOK  (kept for reference — not called by default)
# --------------------------------------------------------------------------- #

def fetch_remoteok(queries: list, cfg: dict) -> list:
    """
    RemoteOK public API: https://remoteok.com/api
    ToS: must credit source with direct link back to listing.
    API is delayed ~24h intentionally.
    """
    print("[search] Fetching RemoteOK API...")
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobHuntBot/1.0 (personal automation)"},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[search] RemoteOK error: {e}")
        return []

    data = resp.json()
    # First item is a legal notice dict, skip it
    jobs_raw = [j for j in data if isinstance(j, dict) and "slug" in j]

    results = []
    for job in jobs_raw:
        title = job.get("position", "")
        company = job.get("company", "")
        url = job.get("url", f"https://remoteok.com/remote-jobs/{job.get('slug', '')}")
        description = job.get("description", "")
        tags = ", ".join(job.get("tags", []))

        if not _matches_query(f"{title} {tags} {description}", queries):
            continue
        if _is_blacklisted(title, company, cfg):
            continue

        # Salary filter
        salary_str = _parse_salary(description)
        if cfg.get("min_salary", 0) > 0 and not salary_str:
            pass  # No salary listed — include by default (can't filter)

        results.append({
            "id": _make_id(url),
            "title": title,
            "company": company,
            "url": url,
            "domain": _extract_domain(url),
            "description": description[:2000],
            "tags": tags,
            "salary": salary_str,
            "source": "remoteok",
        })

    print(f"[search] RemoteOK: {len(results)} matching jobs found")
    return results


# --------------------------------------------------------------------------- #
# We Work Remotely
# --------------------------------------------------------------------------- #

WWR_FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-data-science-jobs.rss",
]


def fetch_weworkremotely(queries: list, cfg: dict) -> list:
    """
    We Work Remotely RSS feeds (public, automation-friendly).
    ToS: attribute links back to WWR.
    """
    print("[search] Fetching We Work Remotely RSS...")
    results = []
    seen_urls = set()

    for feed_url in WWR_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[search] WWR feed error ({feed_url}): {e}")
            continue

        for entry in feed.entries:
            title = entry.get("title", "")
            company = entry.get("author", entry.get("company", ""))
            url = entry.get("link", "")
            description = entry.get("summary", "")

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if not _matches_query(f"{title} {description}", queries):
                continue
            if _is_blacklisted(title, company, cfg):
                continue

            # Extract domain from the actual job URL if embedded
            domain = _extract_domain(url)
            salary_str = _parse_salary(description)

            results.append({
                "id": _make_id(url),
                "title": title,
                "company": company,
                "url": url,
                "domain": domain,
                "description": description[:2000],
                "tags": "",
                "salary": salary_str,
                "source": "weworkremotely",
            })

        time.sleep(0.5)  # Be polite between feed requests

    print(f"[search] We Work Remotely: {len(results)} matching jobs found")
    return results


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Search remote jobs and save to DB")
    parser.add_argument("--query", action="append", dest="queries",
                        help="Search query (can pass multiple times)")
    args = parser.parse_args()

    cfg = load_config()
    queries = args.queries or cfg.get("search_queries", [])

    if not queries:
        print("[search] No queries provided. Add --query or set search_queries in config.json")
        sys.exit(1)

    print(f"[search] Searching for: {queries}")

    init_db()

    all_jobs = []
    all_jobs.extend(fetch_linkedin(queries, cfg))
    all_jobs.extend(fetch_weworkremotely(queries, cfg))

    # Apply max_jobs_per_run limit to *newly saved* jobs, not total fetched.
    # Without this, if most fetched jobs are already in the DB the cap is
    # consumed by duplicates and very few new jobs actually get scored.
    max_jobs = cfg.get("max_jobs_per_run", 50)
    new_count = 0
    for job in all_jobs:
        if new_count >= max_jobs:
            break
        if save_job(job):
            new_count += 1

    print(f"[search] Done. {new_count} new jobs saved ({len(all_jobs)} total matched).")


if __name__ == "__main__":
    main()
