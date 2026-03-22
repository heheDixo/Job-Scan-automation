"""
telegram_bot.py — Telegram webhook server for job approval flow.

Two-step approval:
  1. Bot sends job summary → user taps ✅ Apply or ❌ Skip
  2. If ✅ Apply → bot sends cover letter preview → user taps ✅ Confirm Submit
                                                   or ✏️ I'll Apply Myself
  3. On ✅ Confirm Submit → auto-apply + extract contacts → send results

Run as:
    python tools/telegram_bot.py --serve            # Start webhook server
    python tools/telegram_bot.py --send-matched     # Push all matched jobs to Telegram
    python tools/telegram_bot.py --send "<message>" # Send a plain text message

Requirements in .env:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TELEGRAM_WEBHOOK_URL   (e.g. https://your-ngrok-url.ngrok.io)
"""

import argparse
import json
import os
import subprocess
import sys
import threading

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, get_jobs_by_status, get_job_by_id, mark_applied, mark_skipped, mark_manual

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Telegram API helpers
# --------------------------------------------------------------------------- #

def send_message(text: str, reply_markup: dict = None) -> dict:
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    return resp.json()


def answer_callback(callback_query_id: str, text: str = ""):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                  json={"callback_query_id": callback_query_id, "text": text},
                  timeout=5)


def edit_message_text(chat_id: str, message_id: int, text: str):
    requests.post(f"{TELEGRAM_API}/editMessageText", json={
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=5)


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #

def format_job_message(job: dict) -> str:
    salary = f"\n💰 <b>Salary:</b> {job['salary']}" if job.get("salary") else ""
    source_label = "RemoteOK" if job.get("source") == "remoteok" else "We Work Remotely"
    return (
        f"🎯 <b>Match Score: {job['score']}/100</b>\n\n"
        f"<b>{job['title']}</b> @ {job['company']}\n"
        f"📌 Source: {source_label}{salary}\n\n"
        f"<b>Why it fits:</b>\n{job.get('summary', 'No summary available.')}\n\n"
        f"<b>Good fit:</b> {job.get('why_good_fit', '')}\n"
        f"<b>Concerns:</b> {job.get('concerns', 'None')}\n\n"
        f"🔗 <a href=\"{job['url']}\">View Job Posting</a>"
    )


def format_confirm_message(job: dict) -> str:
    cover = job.get("cover_letter", "No cover letter generated.")
    preview = cover[:600] + "..." if len(cover) > 600 else cover
    return (
        f"📋 <b>Review before submitting:</b>\n\n"
        f"<b>{job['title']}</b> @ {job['company']}\n\n"
        f"<b>Cover Letter Preview:</b>\n<i>{preview}</i>\n\n"
        f"✅ Tap <b>Confirm Submit</b> to auto-apply\n"
        f"✏️ Tap <b>I'll Apply Myself</b> to get the direct link"
    )


def job_keyboard(job_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Apply", "callback_data": f"apply:{job_id}"},
            {"text": "❌ Skip",  "callback_data": f"skip:{job_id}"},
        ]]
    }


def confirm_keyboard(job_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Confirm Submit",     "callback_data": f"confirm:{job_id}"},
            {"text": "✏️ I'll Apply Myself", "callback_data": f"manual:{job_id}"},
        ]]
    }


# --------------------------------------------------------------------------- #
# Action handlers
# --------------------------------------------------------------------------- #

def handle_apply_approved(job_id: str):
    """Step 2: Show cover letter preview and ask for final confirmation."""
    job = get_job_by_id(job_id)
    if not job:
        send_message(f"⚠️ Job {job_id} not found in database.")
        return
    msg = format_confirm_message(job)
    send_message(msg, reply_markup=confirm_keyboard(job_id))


def handle_confirm_submit(job_id: str):
    """Step 3: Run auto-apply + contact extraction."""
    job = get_job_by_id(job_id)
    if not job:
        send_message(f"⚠️ Job {job_id} not found.")
        return

    send_message(f"⏳ Applying to <b>{job['title']}</b> @ {job['company']}...")

    # Run apply_job.py
    result = _run_apply(job)

    if result.get("success"):
        mark_applied(job_id)
        send_message(
            f"✅ <b>Applied!</b>\n{job['title']} @ {job['company']}\n"
            f"Method: {result.get('method', 'auto')}"
        )
        _run_sync_sheets(job_id)
    elif result.get("method") == "captcha_detected":
        send_message(
            f"🔒 <b>CAPTCHA detected</b> — please apply manually:\n"
            f"🔗 <a href=\"{job['url']}\">Apply here</a>"
        )
        mark_manual(job_id)
        _run_sync_sheets(job_id)
    else:
        send_message(
            f"⚠️ Auto-apply failed: {result.get('message', 'Unknown error')}\n"
            f"Please apply manually: <a href=\"{job['url']}\">{job['url']}</a>"
        )
        mark_manual(job_id)
        _run_sync_sheets(job_id)

    # Extract contacts regardless of apply result
    if job.get("domain"):
        _run_contacts(job)


def handle_manual(job_id: str):
    """User wants to apply themselves — send direct link."""
    job = get_job_by_id(job_id)
    if not job:
        send_message(f"⚠️ Job {job_id} not found.")
        return
    mark_manual(job_id)
    send_message(
        f"✏️ <b>Apply yourself:</b>\n"
        f"<a href=\"{job['url']}\">{job['title']} @ {job['company']}</a>"
    )
    _run_sync_sheets(job_id)
    if job.get("domain"):
        _run_contacts(job)


def handle_skip(job_id: str):
    mark_skipped(job_id)
    job = get_job_by_id(job_id)
    title = job["title"] if job else job_id
    send_message(f"❌ Skipped: <b>{title}</b>")


# --------------------------------------------------------------------------- #
# Subprocess runners
# --------------------------------------------------------------------------- #

def _run_apply(job: dict) -> dict:
    import tempfile
    tools_dir = os.path.dirname(__file__)

    # Write cover letter to a temp file to avoid shell arg length limits
    cover_letter = job.get("cover_letter", "")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(cover_letter)
            tmp = f.name

        cmd = [
            sys.executable,
            os.path.join(tools_dir, "apply_job.py"),
            "--url", job["url"],
            "--cover-letter-file", tmp,
            "--headless",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        return {"success": False, "method": "error", "message": str(e)}
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _run_sync_sheets(job_id: str):
    """Sync a single job to Google Sheets (best effort, non-blocking)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return  # Google Sheets not configured — skip silently
    tools_dir = os.path.dirname(__file__)
    try:
        subprocess.run(
            [sys.executable, os.path.join(tools_dir, "sync_sheets.py"),
             "--job-id", job_id],
            capture_output=True, timeout=30
        )
    except Exception:
        pass  # Sheets sync is best-effort; don't break the main flow


def _run_contacts(job: dict):
    tools_dir = os.path.dirname(__file__)
    cmd = [
        sys.executable,
        os.path.join(tools_dir, "extract_contacts.py"),
        "--domain", job["domain"],
        "--company", job["company"],
        "--job-url", job.get("url", ""),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # Extract the JSON array from stdout — search for the last [...] block
        # so that any preceding log lines or legal disclaimers don't break parsing
        import re as _re
        json_match = _re.search(r'(\[.*\])', result.stdout, _re.DOTALL)
        if not json_match:
            return
        contacts = json.loads(json_match.group(1))
        if contacts:
            lines = []
            for c in contacts:
                role = f" ({c['role']})" if c.get('role') else ""
                lines.append(f"• {c['name']}{role}: <code>{c['email']}</code>")
            contacts_text = "\n".join(lines)
            send_message(
                f"📇 <b>Hiring contacts at {job['company']}:</b>\n\n"
                f"{contacts_text}\n\n"
                f"⚠️ Comply with CAN-SPAM/GDPR before cold outreach."
            )
    except Exception as e:
        send_message(f"⚠️ Could not extract contacts for {job['company']}: {e}")


# --------------------------------------------------------------------------- #
# Webhook endpoint
# --------------------------------------------------------------------------- #

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    # Handle inline keyboard callbacks
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        cq_id = cq["id"]
        msg_id = cq["message"]["message_id"]
        chat_id = cq["message"]["chat"]["id"]

        answer_callback(cq_id)

        if ":" in data:
            action, job_id = data.split(":", 1)
        else:
            return jsonify(ok=True)

        if action == "apply":
            edit_message_text(chat_id, msg_id, f"⏳ Loading preview for job {job_id}...")
            threading.Thread(target=handle_apply_approved, args=(job_id,), daemon=True).start()
        elif action == "confirm":
            edit_message_text(chat_id, msg_id, "⏳ Submitting application...")
            threading.Thread(target=handle_confirm_submit, args=(job_id,), daemon=True).start()
        elif action == "manual":
            threading.Thread(target=handle_manual, args=(job_id,), daemon=True).start()
        elif action == "skip":
            handle_skip(job_id)  # Fast DB write — no need to background

    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #

def send_matched_jobs():
    init_db()
    jobs = get_jobs_by_status("matched")
    if not jobs:
        print("[bot] No matched jobs to send.")
        return
    print(f"[bot] Sending {len(jobs)} matched jobs to Telegram...")
    for job in jobs:
        msg = format_job_message(job)
        resp = send_message(msg, reply_markup=job_keyboard(job["id"]))
        if resp.get("ok"):
            print(f"[bot] Sent: {job['title']} @ {job['company']} (score: {job['score']})")
        else:
            print(f"[bot] Failed to send job {job['id']}: {resp}")


def register_webhook():
    if not WEBHOOK_URL:
        print("[bot] ERROR: TELEGRAM_WEBHOOK_URL not set in .env")
        sys.exit(1)
    webhook_endpoint = f"{WEBHOOK_URL}/webhook"
    resp = requests.post(f"{TELEGRAM_API}/setWebhook",
                         json={"url": webhook_endpoint}, timeout=10)
    print(f"[bot] Webhook registered: {resp.json()}")


def main():
    parser = argparse.ArgumentParser(description="Telegram bot for job approvals")
    parser.add_argument("--serve", action="store_true", help="Start webhook server")
    parser.add_argument("--send-matched", action="store_true",
                        help="Send all matched jobs to Telegram")
    parser.add_argument("--send", metavar="TEXT", help="Send a plain text message")
    parser.add_argument("--register-webhook", action="store_true",
                        help="Register webhook URL with Telegram")
    parser.add_argument("--port", type=int, default=None, help="Port for webhook server")
    args = parser.parse_args()

    if not TOKEN:
        print("[bot] ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not CHAT_ID and not args.serve:
        print("[bot] ERROR: TELEGRAM_CHAT_ID not set in .env")
        sys.exit(1)

    if args.register_webhook:
        register_webhook()
    elif args.send_matched:
        send_matched_jobs()
    elif args.send:
        resp = send_message(args.send)
        print(f"[bot] Message sent: {resp.get('ok')}")
    elif args.serve:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        port = args.port or cfg.get("telegram_webhook_port", 8443)
        register_webhook()
        print(f"[bot] Starting webhook server on port {port}...")
        app.run(host="0.0.0.0", port=port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
