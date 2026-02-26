from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, TimedOut, RetryAfter, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .coursera import CourseInfo, canonicalize_url, get_course_info, is_supported_coursera_url
from .text import START_TEXT, HELP_TEXT

# ----------------------------
# Logging (avoid token leaks)
# ----------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("coursera-info-bot")

# Silence noisy libs that may print URLs (bot token can appear inside)
for noisy in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ----------------------------
# Render / Web Service port helper (human-factor resilient)
# If we ever fall back to polling on Render, this keeps the port open.
# ----------------------------
def _start_health_server() -> None:
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
            return

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ----------------------------
# Runtime settings
# ----------------------------
MAX_URLS_PER_MESSAGE = int(os.environ.get("MAX_URLS_PER_MESSAGE", "3"))
FETCH_CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "3"))
FETCH_TIMEOUT_SEC = int(os.environ.get("FETCH_TIMEOUT_SEC", "25"))
_fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)


def escape_html(s: str) -> str:
    import html as _html
    return _html.escape(s or "", quote=True)


def format_info(info: CourseInfo) -> str:
    kind_map = {
        "course": "Course",
        "specialization": "Specialization",
        "professional_certificate": "Professional Certificate",
        "project": "Guided Project",
        "unknown": "Coursera link",
    }

    parts = ["<b>Coursera summary</b>"]
    if info.title:
        parts.append(f"<b>Title:</b> {escape_html(info.title)}")
    parts.append(f"<b>Type:</b> {escape_html(kind_map.get(info.kind, info.kind))}")

    if info.total_hours is not None:
        if info.sum_basis == "modules":
            parts.append(f"<b>Total hours:</b> {info.total_hours:g} (sum of {info.items_count} modules)")
        elif info.sum_basis == "courses":
            parts.append(f"<b>Total hours:</b> {info.total_hours:g} (sum of {info.items_count} courses)")
        else:
            parts.append(f"<b>Total hours:</b> {info.total_hours:g}")
    elif info.workload_hint:
        parts.append(f"<b>Workload hint:</b> {escape_html(info.workload_hint)}")

    if info.course_count is not None:
        parts.append(f"<b>Courses in series:</b> {info.course_count}")

    parts.append(f"<b>Link:</b> {escape_html(info.url)}")
    parts.append("<i>Note: best-effort, uses only public page content.</i>")
    return "\n".join(parts)


async def safe_reply(update: Update, text: str, **kwargs) -> None:
    if not update.message:
        return
    for attempt in range(3):
        try:
            await update.message.reply_text(text, **kwargs)
            return
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except (TimedOut, NetworkError):
            await asyncio.sleep(1.0 + attempt)
        except BadRequest:
            if kwargs.get("parse_mode"):
                kwargs.pop("parse_mode", None)
                await update.message.reply_text(text)
            return


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(update, START_TEXT, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(update, HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""
    urls = re.findall(r"https?://\S+", text)
    urls = [u.rstrip(").,]>'\"") for u in urls]

    seen = set()
    canon_urls = []
    for u in urls:
        cu = canonicalize_url(u)
        if not cu:
            continue
        if cu not in seen:
            seen.add(cu)
            canon_urls.append(cu)

    if not canon_urls:
        await safe_reply(
            update,
            "Send me a Coursera link and I will summarize it.\n\n"
            "Examples:\n"
            "https://www.coursera.org/professional-certificates/adp-airs-entry-level-recruiter\n"
            "https://www.coursera.org/specializations/jhu-data-science\n"
            "https://www.coursera.org/learn/intro-fpga-design-embedded-systems/home/welcome\n\n"
            "⏳ First request may take up to 1 minute.",
        )
        return

    canon_urls = canon_urls[:MAX_URLS_PER_MESSAGE]
    status_msg = await update.message.reply_text("⏳ Checking the page...")

    async def process_one(url: str) -> str:
        if not is_supported_coursera_url(url):
            return f"Unsupported link:\n{url}"
        async with _fetch_sem:
            try:
                info = await asyncio.wait_for(asyncio.to_thread(get_course_info, url), timeout=FETCH_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                return f"❌ Timeout while reading:\n{url}"
            except Exception:
                logger.exception("Failed to parse url: %s", url)
                return f"❌ Could not parse right now:\n{url}"
        return format_info(info)

    results = []
    for u in canon_urls:
        results.append(await process_one(u))

    try:
        await status_msg.edit_text("✅ Done. See results below.")
    except Exception:
        pass

    for msg in results:
        await safe_reply(update, msg, parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.error("Telegram Conflict (two instances). Webhook mode avoids this on Render.")
        return
    logger.exception("Unhandled error: %s", err)
    if isinstance(update, Update) and update.message:
        await safe_reply(update, "❌ Unexpected error. Please try again.")


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Missing TELEGRAM_TOKEN. Set it in Render env vars or .env file.")

    app = Application.builder().token(token).build()
    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    mode = os.environ.get("MODE", "auto").strip().lower()
    base_url = (os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    port = int(os.environ.get("PORT", "10000"))
    url_path = os.environ.get("WEBHOOK_PATH", "telegram").strip("/")

    # Start health server for Render if we are not using webhook
    # (webhook binds the same port itself).
    if not (mode in {"auto", "webhook"} and base_url) and os.environ.get("PORT"):
        threading.Thread(target=_start_health_server, daemon=True).start()

    if mode == "webhook" and not base_url:
        raise SystemExit("MODE=webhook but WEBHOOK_BASE_URL/RENDER_EXTERNAL_URL is missing.")

    if mode in {"auto", "webhook"} and base_url:
        webhook_url = f"{base_url}/{url_path}"
        logger.info("Starting in WEBHOOK mode: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,
        )
        return

    logger.info("Starting in POLLING mode")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


if __name__ == "__main__":
    load_dotenv()
    main()
