# Workflow: AI Job Hunt Automation

## Objective
Automatically search remote jobs daily, score them against your resume using Claude AI,
send you a Telegram summary for approval, auto-apply on your confirmation, and deliver
hiring manager contact info — all without manual effort.

## Required Inputs
- `my_resume.txt` — Your full resume text
- `my_jd.txt` — Your target role description (what you're looking for)
- `.env` — All API keys populated
- `config.json` — Search queries, filters, schedule time

## One-Time Setup (do this before first run)

### 1. Fill in your files
- Replace `my_resume.txt` with your actual resume
- Replace `my_jd.txt` with your target role description
- Edit `config.json` → set `search_queries` to your target roles

### 2. Get API keys (all FREE, no credit card required)
| Service | Where to get it | Free limit | .env key |
|---|---|---|---|
| **Groq** (AI matching) | console.groq.com | 500k tokens/day | `GROQ_API_KEY` |
| **Telegram Bot** | @BotFather → `/newbot` | Unlimited | `TELEGRAM_BOT_TOKEN` |
| **Telegram Chat ID** | Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` | — | `TELEGRAM_CHAT_ID` |
| **Hunter.io** (contacts) | hunter.io/api-keys | 50/month | `HUNTER_API_KEY` |
| **Google Sheets** | Optional — see sync_sheets.py | Unlimited | `GOOGLE_SHEET_ID` |

> **Contact extraction fallback:** When Hunter.io quota is exhausted, the system automatically falls back to scraping the company's website for emails — completely free with no limits.

### 3. Set up Telegram webhook (for real-time button responses)
```bash
# Option A: Local dev with ngrok
ngrok http 8443
# Copy the https URL → set TELEGRAM_WEBHOOK_URL in .env

# Option B: Deploy on a VPS/cloud server
# Set TELEGRAM_WEBHOOK_URL to your server's public HTTPS URL
```

### 4. Initialize the database
```bash
python tools/db.py
```

## Daily Automated Run

### Option A: Scheduler (recommended — keeps running)
```bash
python tools/scheduler.py
# Runs at the time set in config.json → "daily_run_time": "09:00"
```

### Option B: Manual trigger (run pipeline right now)
```bash
python tools/scheduler.py --now
```

## Manual Step-by-Step Run

### Step 1: Search for jobs
```bash
python tools/search_jobs.py --query "python developer" --query "data engineer"
# Saves new matching jobs to .tmp/jobs.db
```

### Step 2: Score jobs with Claude AI
```bash
python tools/match_jobs.py
# Scores all pending jobs, generates cover letters, marks matched/rejected
```

### Step 3: Start Telegram bot server
```bash
python tools/telegram_bot.py --serve
# Starts webhook server; registers URL with Telegram
```

### Step 4: Send matched jobs to Telegram
```bash
python tools/telegram_bot.py --send-matched
# Sends each matched job as a Telegram message with ✅ Apply / ❌ Skip buttons
```

### Step 5: Review and approve in Telegram
- Tap **✅ Apply** → bot shows cover letter preview
- Tap **✅ Confirm Submit** → auto-apply runs + hiring contacts delivered
- Tap **✏️ I'll Apply Myself** → get direct link sent to you
- Tap **❌ Skip** → job marked skipped, never shown again

### Step 6: (Optional) Sync to Google Sheets
```bash
python tools/sync_sheets.py --all
# Logs all applied/manual jobs to your tracking spreadsheet
```

## Tools Reference

| Tool | Purpose | Key output |
|---|---|---|
| `tools/db.py` | Initialize + query SQLite | `.tmp/jobs.db` |
| `tools/search_jobs.py` | Fetch jobs from RemoteOK + WWR | SQLite: status=pending |
| `tools/match_jobs.py` | Claude AI scoring + cover letter | SQLite: status=matched/rejected |
| `tools/apply_job.py` | Playwright browser automation | Screenshot + JSON result |
| `tools/extract_contacts.py` | Hunter.io hiring manager contacts | SQLite cache + JSON |
| `tools/telegram_bot.py` | Bot notifications + approval flow | Real-time Telegram messages |
| `tools/sync_sheets.py` | Google Sheets application tracker | Rows appended to Sheet |
| `tools/scheduler.py` | Daily pipeline runner | Runs full pipeline on schedule |

## config.json Reference

```json
{
  "search_queries": ["python developer", "data engineer"],
  "min_salary": 0,
  "match_threshold": 60,
  "blacklisted_companies": ["CompanyToSkip"],
  "blacklisted_keywords": ["sales", "unpaid"],
  "daily_run_time": "09:00",
  "max_jobs_per_run": 50,
  "telegram_webhook_port": 8443
}
```

## Edge Cases & Troubleshooting

### CAPTCHA detected during auto-apply
Bot automatically falls back to sending you the direct apply URL via Telegram.
No action needed — just click the link and apply manually.

### Hunter.io quota exhausted (25 free/mo)
Contacts are cached in SQLite for 30 days per domain.
The tool will skip API calls for companies already looked up.
Upgrade to a paid Hunter plan if you need more searches.

### Telegram webhook not receiving updates
1. Check `TELEGRAM_WEBHOOK_URL` is a public HTTPS URL (not localhost)
2. If using ngrok, note the URL changes each restart — update `.env` and re-run `--serve`
3. Test with: `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`

### Job form not auto-fillable
Some companies use embedded iframes or custom ATS forms (Greenhouse, Lever).
The bot detects this and sends you the direct URL instead.

### match_jobs.py returns no matched jobs
Lower `match_threshold` in `config.json` (try 50 instead of 60).
Or broaden the `search_queries` to cast a wider net.

## Legal Compliance Notes
- **RemoteOK**: Attribution included in all messages ("via RemoteOK")
- **We Work Remotely**: RSS feed used directly with source attribution
- **Hunter.io contacts**: Sent to you only. Before cold outreach, comply with CAN-SPAM (US) and GDPR (EU/UK)
- **Auto-apply**: Only factual data from your resume is submitted. You confirm before any submission.
- **Platform ToS**: Auto-apply targets public job application forms only, no access controls bypassed
