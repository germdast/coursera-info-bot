import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ============================
# Logging (don't leak tokens)
# ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("coursera-course-info-bot")

# Silence noisy loggers (they may print URLs containing the bot token)
for noisy in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ============================
# Render Web Service needs a port
# ============================
def start_health_server() -> None:
    """Render Web Service expects an open TCP port (env PORT)."""
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

        def log_message(self, format, *args):
            return  # no access logs

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# ============================
# Coursera parsing (public info only)
# ============================
UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    )
}

ALLOWED_PATH_PREFIXES = ("/learn/", "/specializations/", "/professional-certificates/")

# Separators can disappear in extracted text, so we treat them as OPTIONAL.
SEP_OPT = r"(?:[·•\-–—:]\s*)?"

# Examples:
# Module 1 · 2 hours to complete
# Module 1 2 hours to complete
# Course 1 · 40 hours
# Course 1 40 hours
MODULE_HOURS_RE = re.compile(
    rf"Module\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b(?:\s*to\s*complete)?",
    re.IGNORECASE,
)
COURSE_HOURS_RE = re.compile(
    rf"Course\s*\d+\s*{SEP_OPT}(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
    re.IGNORECASE,
)

# Fuzzy fallback if Coursera inserts extra words between labels and hours
MODULE_HOURS_FUZZY_RE = re.compile(
    r"Module\s*\d+.{0,60}?(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
    re.IGNORECASE,
)
COURSE_HOURS_FUZZY_RE = re.compile(
    r"Course\s*\d+.{0,60}?(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
    re.IGNORECASE,
)

GENERIC_HOURS_TO_COMPLETE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*to\s*complete",
    re.IGNORECASE,
)

def _extract_first_url(text: str) -> Optional[str]:
    m = re.search(r"https?://\S+", (text or "").strip())
    if not m:
        return None
    return m.group(0).rstrip(").,]>"'")

def normalize_url(text: str) -> Optional[str]:
    """Also accepts /programs/<program>/... links and canonicalizes them."""
    raw = _extract_first_url(text)
    if not raw:
        return None

    try:
        p = urlparse(raw)
    except Exception:
        return None

    host = (p.netloc or "").lower()
    if not host.endswith("coursera.org"):
        return None

    path = p.path or ""
    # Strip /programs/<slug>/ prefix if present
    if path.startswith("/programs/"):
        parts = path.split("/")
        # ['', 'programs', '{program}', 'specializations', 'slug']
        if len(parts) >= 4:
            path = "/" + "/".join(parts[3:])

    if not any(path.startswith(pref) for pref in ALLOWED_PATH_PREFIXES):
        return None

    return urlunparse(("https", "www.coursera.org", path.rstrip("/"), "", "", ""))

def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text

def _sum_matches(pattern: re.Pattern, text: str) -> float:
    total = 0.0
    for m in pattern.finditer(text):
        try:
            total += float(m.group(1))
        except Exception:
            pass
    return total

def _count_matches(pattern: re.Pattern, text: str) -> int:
    return len(list(pattern.finditer(text)))

def parse_workload(url: str, soup: BeautifulSoup) -> Dict[str, Any]:
    """
    Requirements:
      - If /learn/ (course) -> sum module hours.
      - If /specializations/ or /professional-certificates/ -> sum course hours.
    """
    page_text = soup.get_text(" ", strip=True)

    is_course = "/learn/" in url
    is_spec = "/specializations/" in url
    is_pro = "/professional-certificates/" in url

    workload: Dict[str, Any] = {"total_hours": None, "basis": None, "items": None}

    if is_course:
        total = _sum_matches(MODULE_HOURS_RE, page_text)
        count = _count_matches(MODULE_HOURS_RE, page_text)

        if count == 0:
            total = _sum_matches(MODULE_HOURS_FUZZY_RE, page_text)
            count = _count_matches(MODULE_HOURS_FUZZY_RE, page_text)

        if count > 0 and total > 0:
            workload.update({"total_hours": total, "basis": "modules", "items": count})
            return workload

        m = GENERIC_HOURS_TO_COMPLETE_RE.search(page_text)
        if m:
            workload.update({"total_hours": float(m.group(1)), "basis": "hint", "items": None})
        return workload

    if is_spec or is_pro:
        total = _sum_matches(COURSE_HOURS_RE, page_text)
        count = _count_matches(COURSE_HOURS_RE, page_text)

        if count == 0:
            total = _sum_matches(COURSE_HOURS_FUZZY_RE, page_text)
            count = _count_matches(COURSE_HOURS_FUZZY_RE, page_text)

        if count > 0 and total > 0:
            workload.update({"total_hours": total, "basis": "courses", "items": count})
            return workload

        m = GENERIC_HOURS_TO_COMPLETE_RE.search(page_text)
        if m:
            workload.update({"total_hours": float(m.group(1)), "basis": "hint", "items": None})
        return workload

    return workload

def parse_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.text:
        return soup.title.text.strip()
    return "Coursera"

def page_type(url: str) -> str:
    if "/specializations/" in url:
        return "Specialization"
    if "/professional-certificates/" in url:
        return "Professional Certificate"
    return "Course"

def fmt_hours(x: float) -> str:
    if x is None:
        return "-"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.1f}"

def format_info(info: Dict[str, Any]) -> str:
    title = info.get("title") or "Coursera"
    kind = info.get("type") or "-"
    url = info.get("url") or ""

    lines: List[str] = []
    lines.append(f"✅ {title}")
    lines.append(f"Type: {kind}")

    wl = info.get("workload") or {}
    total = wl.get("total_hours")

    if total is not None:
        if wl.get("basis") == "modules":
            lines.append(f"Total hours: {fmt_hours(total)} (sum of {wl.get('items')} modules)")
        elif wl.get("basis") == "courses":
            lines.append(f"Total hours: {fmt_hours(total)} (sum of {wl.get('items')} courses)")
        else:
            lines.append(f"Total hours: {fmt_hours(total)} (mentioned on page)")
    else:
        lines.append("Total hours: - (not found)")

    lines.append(f"Link: {url}")
    lines.append("")
    lines.append("ℹ️ Bot reads only publicly available information shown on the page.")
    return "\n".join(lines)

# ============================
# Telegram bot
# ============================
WELCOME = (
    "Hi! Send a Coursera link and I will return a short summary.\n\n"
    "Supported:\n"
    "• /learn/... (course)\n"
    "• /specializations/... (specialization)\n"
    "• /professional-certificates/... (professional certificate)\n"
    "• /programs/.../learn|specializations|professional-certificates/... (program links)\n"
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    url = normalize_url(update.message.text)
    if not url:
        await update.message.reply_text(
            "Please send a valid Coursera link (course/specialization/professional-certificate)."
        )
        return

    await update.message.reply_text("⏳ Checking the page...")

    try:
        html = fetch_html(url)
        soup = BeautifulSoup(html, "lxml")

        info = {
            "title": parse_title(soup),
            "type": page_type(url),
            "url": url,
            "workload": parse_workload(url, soup),
        }

        await update.message.reply_text(format_info(info))

    except requests.HTTPError:
        await update.message.reply_text("❌ I could not access this page right now. Please try again later.")
    except Exception:
        logger.exception("Unexpected error while parsing")
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
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
