"""
scheduler.py — Daily job hunt pipeline runner.

Runs the full pipeline once per day at the configured time:
  1. search_jobs.py  — fetch new remote jobs
  2. match_jobs.py   — score against your resume
  3. telegram_bot.py — push matched jobs to Telegram for approval

Usage:
    python tools/scheduler.py           # Run on schedule (keeps running)
    python tools/scheduler.py --now     # Run pipeline immediately (once)

Configure run time in config.json → "daily_run_time": "09:00"
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import schedule
import time

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOOLS_DIR = os.path.dirname(__file__)


def _run_tool(script_name: str, extra_args: list = None) -> bool:
    """Run a tool script as a subprocess. Returns True on success."""
    cmd = [sys.executable, os.path.join(TOOLS_DIR, script_name)]
    if extra_args:
        cmd.extend(extra_args)

    print(f"\n[scheduler] ▶ Running {script_name}...")
    result = subprocess.run(cmd, capture_output=False, text=True)
    success = result.returncode == 0
    status = "✅ OK" if success else f"❌ FAILED (exit {result.returncode})"
    print(f"[scheduler] {script_name} → {status}")
    return success


def _notify_telegram(message: str):
    """Send a notification to Telegram (best effort)."""
    try:
        subprocess.run(
            [sys.executable, os.path.join(TOOLS_DIR, "telegram_bot.py"),
             "--send", message],
            capture_output=True, timeout=15
        )
    except Exception:
        pass


def run_pipeline():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    queries = cfg.get("search_queries", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*50}")
    print(f"[scheduler] Pipeline started at {now}")
    print(f"[scheduler] Queries: {queries}")
    print(f"{'='*50}")

    _notify_telegram(f"🤖 Job hunt pipeline started\n📅 {now}\n🔍 Queries: {', '.join(queries)}")

    # Step 1: Search
    search_args = []
    for q in queries:
        search_args.extend(["--query", q])
    ok_search = _run_tool("search_jobs.py", search_args)

    if not ok_search:
        _notify_telegram("⚠️ Job search step failed. Check logs.")
        return

    # Step 2: Match
    ok_match = _run_tool("match_jobs.py")
    if not ok_match:
        _notify_telegram("⚠️ Job matching step failed. Check logs.")
        return

    # Step 3: Send matched jobs to Telegram
    ok_send = _run_tool("telegram_bot.py", ["--send-matched"])

    summary_msg = (
        f"✅ Pipeline complete!\n"
        f"📅 {now}\n"
        f"Check the messages above to approve or skip jobs."
    )
    _notify_telegram(summary_msg)
    print(f"\n[scheduler] Pipeline complete at {datetime.now().strftime('%H:%M:%S')}")


def main():
    parser = argparse.ArgumentParser(description="Scheduled daily job hunt pipeline")
    parser.add_argument("--now", action="store_true",
                        help="Run pipeline immediately instead of waiting for schedule")
    args = parser.parse_args()

    if args.now:
        run_pipeline()
        return

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    run_time = cfg.get("daily_run_time", "09:00")
    print(f"[scheduler] Scheduled to run daily at {run_time}")
    print(f"[scheduler] Current time: {datetime.now().strftime('%H:%M')}")
    print(f"[scheduler] Press Ctrl+C to stop.\n")

    schedule.every().day.at(run_time).do(run_pipeline)

    while True:
        schedule.run_pending()
        time.sleep(30)  # Check every 30 seconds


if __name__ == "__main__":
    main()
