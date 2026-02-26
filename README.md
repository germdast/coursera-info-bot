# Coursera Course Info Bot (Telegram)

A small **Telegram bot in Python** that reads **publicly available information** from Coursera course pages and returns a quick overview (title, type, estimated workload, and—when possible—the number of courses in a specialization).

https://t.me/courserainfobot

✅ **What this project is**
- A learning project for:
  - Telegram Bot API (python-telegram-bot)
  - HTTP requests + HTML parsing (requests + BeautifulSoup)
  - basic text extraction & regex
  - deployment with Docker (works well on Render)

🚫 **What this project is NOT**
- Not affiliated with Coursera.
- Not a tool for bypassing payments, accounts, or certificates.
- The bot only uses **public page content** and does not require login.

---

## Features
- Detects Coursera links:
  - `/learn/...` (course)
  - `/specializations/...` (specialization)
  - `/professional-certificates/...` (professional certificate)
  - `/projects/...` (guided project)
- Extracts:
  - title (best-effort)
  - short description (best-effort)
  - workload hints like "X hours" / "X weeks" (best-effort)
  - course count for series (best-effort)

> Note: Coursera page structure can change. Extraction is best-effort and may fail sometimes.

---

## Quick start (local)

### 1) Create a bot token
Create a Telegram bot via **@BotFather** and copy the token.

### 2) Setup environment variables
Create `.env` (or set env vars in your OS):

```bash
TELEGRAM_TOKEN="YOUR_TOKEN_HERE"
```

### 3) Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

### 4) Run

```bash
python -m src.bot
```

---

## Run with Docker

```bash
docker build -t coursera-course-info-bot .
docker run --rm -e TELEGRAM_TOKEN="YOUR_TOKEN_HERE" coursera-course-info-bot
```

---

## Deploy on Render (simple)
- Use **Docker** deploy
- Set env var:
  - `TELEGRAM_TOKEN`

---

## Security notes
- **Never commit tokens** into Git.
- Use environment variables.

If you accidentally committed a secret (token), rotate it in BotFather and remove it from Git history.

---

## License
MIT (you can change/remove if you want).
