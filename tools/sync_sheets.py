"""
sync_sheets.py — Log job applications to Google Sheets for dashboard visibility.

Each row: Date | Company | Title | Score | Status | Apply URL | Contacts Found | Source

Setup:
  1. Create a Google Sheet
  2. Create a GCP service account, download credentials JSON, save as credentials.json in project root
  3. Share the Google Sheet with the service account email (Editor access)
  4. Set GOOGLE_SHEET_ID in .env

Usage:
    python tools/sync_sheets.py --job-id <job_id>      # Sync a single job
    python tools/sync_sheets.py --all                   # Sync all applied/manual jobs
"""

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_jobs_by_status, get_job_by_id, get_cached_contacts

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_NAME = "Applications"

HEADERS = [
    "Date Applied", "Company", "Title", "Score", "Status",
    "Apply URL", "Source", "Contacts Found", "Cover Letter Preview"
]


def _get_sheet():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[sheets] ERROR: Run: pip install gspread google-auth")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_PATH):
        print(f"[sheets] ERROR: credentials.json not found at {CREDENTIALS_PATH}")
        print("[sheets] Download a GCP service account key and save it as credentials.json")
        sys.exit(1)

    if not SHEET_ID:
        print("[sheets] ERROR: GOOGLE_SHEET_ID not set in .env")
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def _ensure_sheet_headers(worksheet):
    existing = worksheet.row_values(1)
    if existing != HEADERS:
        worksheet.insert_row(HEADERS, 1)
        worksheet.format("A1:I1", {"textFormat": {"bold": True}})
        print("[sheets] Headers written.")


def _get_or_create_worksheet(spreadsheet):
    try:
        return spreadsheet.worksheet(SHEET_NAME)
    except Exception:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows="1000", cols="20")
        return ws


def _job_to_row(job: dict) -> list:
    # Get cached contacts for this company domain
    contact_emails = ""
    if job.get("domain"):
        contacts = get_cached_contacts(job["domain"])
        if contacts:
            contact_emails = ", ".join(c["email"] for c in contacts[:3])

    cover_preview = ""
    if job.get("cover_letter"):
        cover_preview = job["cover_letter"][:150] + "..."

    return [
        job.get("date_applied") or datetime.utcnow().strftime("%Y-%m-%d"),
        job.get("company", ""),
        job.get("title", ""),
        job.get("score", 0),
        job.get("status", ""),
        job.get("url", ""),
        job.get("source", ""),
        contact_emails,
        cover_preview,
    ]


def sync_job(job_id: str):
    init_db()
    job = get_job_by_id(job_id)
    if not job:
        print(f"[sheets] Job {job_id} not found in database.")
        return

    spreadsheet = _get_sheet()
    ws = _get_or_create_worksheet(spreadsheet)
    _ensure_sheet_headers(ws)

    # Check if this URL is already logged
    all_urls = ws.col_values(6)  # Column F = Apply URL
    if job["url"] in all_urls:
        print(f"[sheets] Already logged: {job['title']} @ {job['company']}")
        return

    row = _job_to_row(job)
    ws.append_row(row)
    print(f"[sheets] Logged: {job['title']} @ {job['company']}")


def sync_all():
    init_db()
    spreadsheet = _get_sheet()
    ws = _get_or_create_worksheet(spreadsheet)
    _ensure_sheet_headers(ws)

    all_urls = set(ws.col_values(6)[1:])  # Skip header

    for status in ("applied", "manual"):
        jobs = get_jobs_by_status(status)
        for job in jobs:
            if job["url"] in all_urls:
                continue
            row = _job_to_row(job)
            ws.append_row(row)
            all_urls.add(job["url"])
            print(f"[sheets] Logged: {job['title']} @ {job['company']} ({status})")

    print("[sheets] Sync complete.")


def main():
    parser = argparse.ArgumentParser(description="Sync applications to Google Sheets")
    parser.add_argument("--job-id", help="Sync a specific job by ID")
    parser.add_argument("--all", action="store_true", help="Sync all applied/manual jobs")
    args = parser.parse_args()

    if args.job_id:
        sync_job(args.job_id)
    elif args.all:
        sync_all()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
