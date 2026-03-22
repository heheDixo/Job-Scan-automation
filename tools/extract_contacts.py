"""
extract_contacts.py — Find hiring manager / recruiter contacts.

Strategy (in order):
  1. Hunter.io API (free tier: 50 searches/month — https://hunter.io/api-keys)
  2. Web scraping fallback if Hunter.io returns nothing (completely free, no key)

Results are cached in SQLite for 30 days per domain.

Usage:
    python tools/extract_contacts.py --domain "stripe.com"
    python tools/extract_contacts.py --domain "stripe.com" --job-url "https://..."
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.robotparser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_cached_contacts, save_contacts

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

HUNTER_API_BASE = "https://api.hunter.io/v2"

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

CONTACT_PATHS = ["/contact", "/about", "/team", "/about-us", "/careers", "/people"]

HR_KEYWORDS = [
    "recruiter", "recruiting", "talent", "hiring", "hr ",
    "human resources", "people ops", "talent acquisition",
]

IGNORED_EMAIL_DOMAINS = {"example.com", "test.com", "sentry.io", "schema.org"}
IGNORED_EMAIL_PREFIXES = {
    "noreply", "no-reply", "support", "info", "hello", "admin",
    "contact", "team", "jobs", "careers", "apply", "privacy", "legal",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    )
}


# --------------------------------------------------------------------------- #
# Hunter.io (primary — free tier, 50 searches/month)
# --------------------------------------------------------------------------- #

def _is_hr_role(role: str) -> bool:
    return any(kw in role.lower() for kw in HR_KEYWORDS) if role else False


def fetch_from_hunter(domain: str) -> list:
    api_key = os.getenv("HUNTER_API_KEY")
    if not api_key:
        return []  # Fall through to web scraping

    print(f"[contacts] Querying Hunter.io for {domain}...")
    try:
        resp = requests.get(
            f"{HUNTER_API_BASE}/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": 10},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[contacts] Hunter.io error: {e} — falling back to web scraping")
        return []

    data = resp.json()

    # Check for quota errors
    errors = data.get("errors", [])
    if errors:
        print(f"[contacts] Hunter.io: {errors[0].get('details', 'API error')} — falling back to web scraping")
        return []

    emails_data = data.get("data", {}).get("emails", [])
    contacts = []
    for item in emails_data:
        contact = {
            "name": f"{item.get('first_name', '')} {item.get('last_name', '')}".strip(),
            "email": item.get("value", ""),
            "role": item.get("position", ""),
            "linkedin_url": item.get("linkedin", ""),
        }
        if contact["email"]:
            contacts.append(contact)

    # Sort: HR roles first
    contacts.sort(key=lambda c: 0 if _is_hr_role(c["role"]) else 1)
    return contacts[:5]


# --------------------------------------------------------------------------- #
# Web scraping fallback (completely free)
# --------------------------------------------------------------------------- #

def _fetch_page(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _robots_allowed(domain: str, path: str) -> bool:
    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        rp.read()
        return rp.can_fetch("*", f"https://{domain}{path}")
    except Exception:
        return True


def _extract_emails(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    mailto_emails = [
        a["href"][7:].split("?")[0].strip()
        for a in soup.find_all("a", href=True)
        if a["href"].startswith("mailto:")
    ]

    found = set(EMAIL_REGEX.findall(text)) | set(mailto_emails)
    clean = []
    for email in found:
        local, domain = email.rsplit("@", 1)
        if domain.lower() in IGNORED_EMAIL_DOMAINS:
            continue
        if local.lower() in IGNORED_EMAIL_PREFIXES:
            continue
        if len(local) < 2:
            continue
        clean.append(email.lower())
    return list(set(clean))


def _guess_role(email: str, html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ").lower()
    idx = text.find(email.lower())
    if idx == -1:
        return ""
    context = text[max(0, idx - 200):idx + 200]
    for kw in HR_KEYWORDS:
        if kw in context:
            return kw.strip().title()
    return ""


def scrape_contacts(domain: str, job_url: str = "") -> list:
    all_contacts = []
    seen = set()

    # Scrape job posting page first
    if job_url:
        html = _fetch_page(job_url)
        if html:
            for email in _extract_emails(html):
                if email not in seen:
                    seen.add(email)
                    all_contacts.append({
                        "name": "", "email": email,
                        "role": _guess_role(email, html), "linkedin_url": "",
                    })

    # Scrape company website pages
    for path in CONTACT_PATHS:
        if not _robots_allowed(domain, path):
            continue
        html = _fetch_page(f"https://{domain}{path}")
        if not html:
            time.sleep(0.5)
            continue
        for email in _extract_emails(html):
            if email not in seen:
                seen.add(email)
                all_contacts.append({
                    "name": "", "email": email,
                    "role": _guess_role(email, html), "linkedin_url": "",
                })
        time.sleep(1)
        if len(all_contacts) >= 10:
            break

    # Sort: HR roles first, cap at 5
    all_contacts.sort(key=lambda c: 0 if _is_hr_role(c["role"]) else 1)
    return all_contacts[:5]


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #

def get_contacts(domain: str, job_url: str = "") -> list:
    init_db()

    cached = get_cached_contacts(domain, max_age_days=30)
    if cached:
        print(f"[contacts] Using cached results for {domain} ({len(cached)} contacts)")
        return [{"name": c["name"], "email": c["email"],
                 "role": c["role"], "linkedin_url": c.get("linkedin_url", "")}
                for c in cached]

    # Primary: Hunter.io
    contacts = fetch_from_hunter(domain)

    # Fallback: web scraping
    if not contacts:
        print(f"[contacts] Falling back to web scraping for {domain}...")
        contacts = scrape_contacts(domain, job_url)

    if not contacts:
        print(f"[contacts] No contacts found for {domain}")
        return []

    save_contacts(domain, contacts)
    print(f"[contacts] Found and cached {len(contacts)} contacts for {domain}")
    return contacts


DISCLAIMER = """
⚠️  LEGAL REMINDER before cold outreach:
• CAN-SPAM (US): Include your address + opt-out in every email
• GDPR (EU/UK): Legitimate interest basis required; disclose data source
"""


def main():
    parser = argparse.ArgumentParser(description="Find hiring contacts (Hunter.io + web scraping)")
    parser.add_argument("--domain", required=True, help="Company domain (e.g. stripe.com)")
    parser.add_argument("--company", default="", help="Company name (display only)")
    parser.add_argument("--job-url", default="", help="Job posting URL (also scraped)")
    args = parser.parse_args()

    contacts = get_contacts(args.domain, args.job_url)

    if not contacts:
        print(json.dumps([]))
        sys.exit(0)

    company_label = args.company or args.domain
    print(f"\n[contacts] Results for {company_label}:")
    for c in contacts:
        role_str = f" ({c['role']})" if c.get("role") else ""
        print(f"  • {c['name'] or 'Unknown'}{role_str}: {c['email']}")

    print(DISCLAIMER)
    print(json.dumps(contacts, indent=2))


if __name__ == "__main__":
    main()
