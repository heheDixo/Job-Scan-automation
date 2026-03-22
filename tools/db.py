"""
db.py — SQLite helper for job hunt automation.
Tables: jobs, contacts
Run directly to initialize: python tools/db.py
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", ".tmp", "jobs.db")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create tables if they don't exist."""
    conn = _connect()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            company       TEXT NOT NULL,
            url           TEXT UNIQUE NOT NULL,
            domain        TEXT,
            description   TEXT,
            tags          TEXT,
            salary        TEXT,
            score         INTEGER DEFAULT 0,
            status        TEXT DEFAULT 'pending',
            summary       TEXT,
            why_good_fit  TEXT,
            concerns      TEXT,
            cover_letter  TEXT,
            source        TEXT,
            date_found    TEXT,
            date_applied  TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            company_domain TEXT NOT NULL,
            name          TEXT,
            email         TEXT NOT NULL,
            role          TEXT,
            linkedin_url  TEXT,
            cached_at     TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    print(f"[db] Initialized at {DB_PATH}")


def save_job(job: dict):
    """Insert a new job; skip if URL already exists."""
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO jobs
            (id, title, company, url, domain, description, tags, salary, source, date_found)
        VALUES
            (:id, :title, :company, :url, :domain, :description, :tags, :salary, :source, :date_found)
    """, {
        "id": job["id"],
        "title": job["title"],
        "company": job["company"],
        "url": job["url"],
        "domain": job.get("domain", ""),
        "description": job.get("description", ""),
        "tags": job.get("tags", ""),
        "salary": job.get("salary", ""),
        "source": job.get("source", ""),
        "date_found": datetime.utcnow().isoformat(),
    })
    inserted = c.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def update_job_match(job_id: str, score: int, summary: str, why_good_fit: str,
                     concerns: str, cover_letter: str, status: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("""
        UPDATE jobs SET score=?, summary=?, why_good_fit=?, concerns=?,
                        cover_letter=?, status=?
        WHERE id=?
    """, (score, summary, why_good_fit, concerns, cover_letter, status, job_id))
    conn.commit()
    conn.close()


def mark_applied(job_id: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='applied', date_applied=? WHERE id=?",
              (datetime.utcnow().isoformat(), job_id))
    conn.commit()
    conn.close()


def mark_skipped(job_id: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='skipped' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def mark_manual(job_id: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE jobs SET status='manual', date_applied=? WHERE id=?",
              (datetime.utcnow().isoformat(), job_id))
    conn.commit()
    conn.close()


def get_jobs_by_status(status: str) -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE status=? ORDER BY score DESC", (status,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_job_by_id(job_id: str) -> dict | None:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_cached_contacts(domain: str, max_age_days: int = 30) -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT * FROM contacts
        WHERE company_domain=?
          AND datetime(cached_at) > datetime('now', ?)
    """, (domain, f"-{max_age_days} days"))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def save_contacts(domain: str, contacts: list):
    conn = _connect()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    for contact in contacts:
        c.execute("""
            INSERT INTO contacts (company_domain, name, email, role, linkedin_url, cached_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (domain, contact.get("name", ""), contact["email"],
              contact.get("role", ""), contact.get("linkedin_url", ""), now))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("[db] Tables ready.")
