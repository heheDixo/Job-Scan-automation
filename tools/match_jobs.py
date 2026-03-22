"""
match_jobs.py — Score pending jobs against user's resume using Groq API (FREE).
Generates a match score, 3-bullet summary, and a custom cover letter per job.

Free API: console.groq.com — no credit card needed.
Model: llama-3.3-70b-versatile (free tier: 6,000 tokens/min, 500,000 tokens/day — best free quality)

Usage:
    python tools/match_jobs.py
"""

import json
import os
import sys
import time

from groq import Groq
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_jobs_by_status, update_job_match

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
RESUME_PATH = os.path.join(BASE_DIR, "my_resume.txt")
JD_PATH = os.path.join(BASE_DIR, "my_jd.txt")

SYSTEM_PROMPT = """You are a career coach and recruiter evaluating job-candidate fit.
You receive a candidate's resume and target role profile, then evaluate a job posting.
Always respond with valid JSON only — no markdown, no explanation outside the JSON."""

USER_PROMPT_TEMPLATE = """
## Candidate Resume
{resume}

## Candidate's Target Role (ideal JD)
{target_jd}

## Job Posting to Evaluate
Title: {title}
Company: {company}
Description:
{description}

---
Evaluate this job posting and return a JSON object with these exact keys:
{{
  "score": <integer 0-100, how well this job matches the candidate>,
  "summary": [
    "<bullet 1: most relevant aspect>",
    "<bullet 2: key requirement>",
    "<bullet 3: compensation or culture note if available, else role highlight>"
  ],
  "why_good_fit": "<1-2 sentences on why the candidate is a strong match>",
  "concerns": "<1 sentence on the biggest gap or concern, or 'None' if no concerns>",
  "cover_letter": "<3-paragraph professional cover letter tailored to this specific job. Use the candidate's actual experience from the resume. Do not include placeholders.>"
}}
"""


def load_file(path: str, label: str) -> str:
    if not os.path.exists(path):
        print(f"[match] ERROR: {label} file not found at {path}")
        print(f"[match] Please create the file and try again.")
        sys.exit(1)
    with open(path) as f:
        return f.read().strip()


def score_job(client: Groq, resume: str, target_jd: str, job: dict) -> dict:
    prompt = USER_PROMPT_TEMPLATE.format(
        resume=resume,
        target_jd=target_jd,
        title=job["title"],
        company=job["company"],
        description=job.get("description", "No description available.")[:3000],
    )

    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    raw = completion.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # The LLM sometimes embeds literal newlines/control chars inside JSON string values,
    # making the JSON invalid. Fix: replace control chars inside strings with space,
    # while preserving structural newlines between JSON keys.
    import re
    # Replace actual newline/tab/carriage-return inside JSON string values with a space
    # (strings are everything between unescaped quotes)
    def clean_json_strings(s):
        result = []
        in_string = False
        escaped = False
        for ch in s:
            if escaped:
                result.append(ch)
                escaped = False
            elif ch == '\\' and in_string:
                result.append(ch)
                escaped = True
            elif ch == '"':
                result.append(ch)
                in_string = not in_string
            elif in_string and ord(ch) < 0x20:
                # Replace bare control chars inside strings with space
                result.append(' ')
            else:
                result.append(ch)
        return ''.join(result)

    raw = clean_json_strings(raw)

    return json.loads(raw)


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    threshold = cfg.get("match_threshold", 60)

    resume = load_file(RESUME_PATH, "Resume (my_resume.txt)")
    target_jd = load_file(JD_PATH, "Target JD (my_jd.txt)")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("[match] ERROR: GROQ_API_KEY not set in .env")
        print("[match] Get a free key at: https://console.groq.com (no credit card needed)")
        sys.exit(1)

    client = Groq(api_key=api_key)

    init_db()
    pending_jobs = get_jobs_by_status("pending")

    if not pending_jobs:
        print("[match] No pending jobs to score.")
        return

    print(f"[match] Scoring {len(pending_jobs)} jobs (threshold: {threshold})...")

    matched = 0
    rejected = 0

    for i, job in enumerate(pending_jobs, 1):
        print(f"[match] [{i}/{len(pending_jobs)}] {job['title']} @ {job['company']}...", end=" ")

        try:
            result = score_job(client, resume, target_jd, job)
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e} — skipping")
            continue
        except Exception as e:
            err = str(e)
            if "rate_limit" in err.lower():
                # Parse actual wait time from error: "Please try again in Xm Ys"
                wait_seconds = 65  # default
                import re as _re
                m = _re.search(r'try again in (\d+)m([\d.]+)s', err)
                if m:
                    wait_seconds = int(m.group(1)) * 60 + float(m.group(2)) + 5
                elif "tokens per day" in err.lower():
                    print(f"\n[match] Daily token limit exhausted — stopping. Run again tomorrow or tomorrow the quota resets.")
                    break
                print(f"Rate limit hit — waiting {int(wait_seconds)}s...")
                time.sleep(wait_seconds)
                try:
                    result = score_job(client, resume, target_jd, job)
                except Exception as e2:
                    print(f"API error after retry: {e2} — skipping")
                    continue
            else:
                print(f"API error: {e} — skipping")
                continue

        score = result.get("score", 0)
        status = "matched" if score >= threshold else "rejected"

        summary_bullets = result.get("summary", [])
        summary_str = "\n".join(f"• {b}" for b in summary_bullets)

        update_job_match(
            job_id=job["id"],
            score=score,
            summary=summary_str,
            why_good_fit=result.get("why_good_fit", ""),
            concerns=result.get("concerns", ""),
            cover_letter=result.get("cover_letter", ""),
            status=status,
        )

        if status == "matched":
            matched += 1
            print(f"✅ Score: {score} → MATCHED")
        else:
            rejected += 1
            print(f"❌ Score: {score} → rejected")

    print(f"\n[match] Done. {matched} matched, {rejected} rejected.")
    if matched > 0:
        print("[match] Run telegram_bot.py to send matched jobs for approval.")


if __name__ == "__main__":
    main()
