"""
search_jobs.py — Fetch remote jobs from RemoteOK and We Work Remotely.
Saves new jobs to SQLite. Skips duplicates and blacklisted entries.

Usage:
    python tools/search_jobs.py --query "python developer"
    python tools/search_jobs.py --query "data engineer" --query "backend developer"
"""

import argparse
import hashlib
import json
import os
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
# RemoteOK
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
    all_jobs.extend(fetch_remoteok(queries, cfg))
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
