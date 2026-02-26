import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ----------------------------
# Logging (важно: не светим токен)
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("coursera-course-info-bot")

# Отключаем подробные логи httpx/telegram, где может появляться URL с токеном
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# ----------------------------
# Render healthcheck (открывает порт)
# ----------------------------
def start_health_server() -> None:
    """
    Render Web Service ожидает открытый порт ($PORT).
    Этот сервер просто отвечает "OK" на / и /healthz.
    """
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not Found")

        # отключаем стандартные access-логи
        def log_message(self, format, *args):
            return

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

# Стартуем healthcheck в фоне, чтобы Render был доволен
threading.Thread(target=start_health_server, daemon=True).start()

# ----------------------------
# Utils: Coursera page parsing (только публичная информация)
# ----------------------------
COURSE_URL_RE = re.compile(r"^https?://(www\.)?coursera\.org/(learn|specializations|professional-certificates)/[A-Za-z0-9\-_]+")

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}

def normalize_url(text: str) -> Optional[str]:
    text = (text or "").strip()
    # вытащим первую ссылку из текста
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0).rstrip(").,]")
    if not COURSE_URL_RE.match(url):
        return None
    return url

def fetch_html(url: str, timeout: int = 15) -> str:
    resp = requests.get(url, headers=UA, timeout=timeout)
    resp.raise_for_status()
    return resp.text

def parse_course_info(url: str, html: str) -> Dict[str, Any]:
    """
    Пытаемся достать базовые поля со страницы Coursera.
    Coursera меняет верстку, поэтому делаем максимально "гибко".
    """
    soup = BeautifulSoup(html, "lxml")

    title = None
    # 1) OpenGraph
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    # 2) <title>
    if not title and soup.title and soup.title.text:
        title = soup.title.text.strip()

    # тип страницы по URL
    kind = "Course"
    if "/specializations/" in url:
        kind = "Specialization"
    elif "/professional-certificates/" in url:
        kind = "Professional Certificate"
    elif "/learn/" in url:
        kind = "Course"

    # Примерные “оценки” по нагрузке/неделям — если есть в тексте
    text = soup.get_text(" ", strip=True)
    workload = None
    # часто встречается "X hours" / "X hours per week" / "Approx. X hours"
    m = re.search(r"(\d+)\s*(hour|hours)\b", text, re.IGNORECASE)
    if m:
        workload = f"{m.group(1)} hours (mentioned on page)"

    # кол-во курсов (для specialization) — иногда в JSON-LD или тексте
    course_count = None
    if kind in ("Specialization", "Professional Certificate"):
        m2 = re.search(r"(\d+)\s*(course|courses)\b", text, re.IGNORECASE)
        if m2:
            course_count = int(m2.group(1))

    return {
        "title": title or "Coursera page",
        "type": kind,
        "workload_hint": workload,
        "course_count_hint": course_count,
        "url": url
    }

def format_info(info: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"✅ {info.get('title', 'Coursera page')}")
    lines.append(f"Type: {info.get('type', '-')}")
    if info.get("course_count_hint"):
        lines.append(f"Courses (hint): {info['course_count_hint']}")
    if info.get("workload_hint"):
        lines.append(f"Workload (hint): {info['workload_hint']}")
    lines.append(f"Link: {info.get('url','')}")
    lines.append("")
    lines.append("ℹ️ I read only publicly available info from the page.")
    return "\n".join(lines)

# ----------------------------
# Telegram handlers
# ----------------------------
WELCOME = (
    "Hi! Send me a Coursera course/specialization link and I will return a short info summary.\n\n"
    "Examples:\n"
    "https://www.coursera.org/learn/python\n"
    "https://www.coursera.org/specializations/machine-learning\n"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message.text if update.message else ""
    url = normalize_url(msg)
    if not url:
        await update.message.reply_text("Please send a valid Coursera link (course/specialization/professional-certificate).")
        return

    await update.message.reply_text("⏳ Checking the page...")

    try:
        html = fetch_html(url)
        info = parse_course_info(url, html)
        await update.message.reply_text(format_info(info))
    except requests.HTTPError as e:
        logger.warning("HTTPError: %s", str(e))
        await update.message.reply_text("❌ I could not access this page right now. Please try again later.")
    except Exception as e:
        logger.exception("Unexpected error: %s", str(e))
        await update.message.reply_text("❌ Something went wrong while parsing the page.")

def main() -> None:
    load_dotenv()

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in Render Environment Variables.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    # polling (подходит для Render)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
