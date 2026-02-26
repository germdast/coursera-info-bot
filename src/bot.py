from __future__ import annotations

import logging
import os
import re
from typing import List

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from .coursera import get_course_info, detect_kind
from .text import START_TEXT, HELP_TEXT


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("coursera-course-info-bot")


COURSE_URL_RE = re.compile(r"https?://(?:www\.)?coursera\.org/[\w\-/\?=&%#.]+", re.IGNORECASE)


def extract_urls(text: str) -> List[str]:
    urls = COURSE_URL_RE.findall(text or "")
    # De-duplicate but keep order
    seen = set()
    result = []
    for u in urls:
        u = u.strip().rstrip(")].,\"'")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def format_info(info) -> str:
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

    parts.append(f"<b>Type:</b> {kind_map.get(info.kind, escape_html(info.kind))}")

    if info.workload:
        parts.append(f"<b>Workload hint:</b> {escape_html(info.workload)}")

    if info.course_count is not None:
        parts.append(f"<b>Courses in series:</b> {info.course_count}")

    parts.append(f"<b>Link:</b> {escape_html(info.url)}")

    parts.append("<i>Note: This is best-effort and uses only public page content.</i>")

    return "\n".join(parts)


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""
    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text(
            "Send me a Coursera link and I will try to summarize it.\n\n"
            "Example: https://www.coursera.org/learn/machine-learning",
        )
        return

    for url in urls[:3]:  # avoid spamming / rate limits
        kind = detect_kind(url)
        if not kind:
            await update.message.reply_text(
                f"I found a Coursera link, but it does not look like a supported course page:\n{url}"
            )
            continue

        info = get_course_info(url)
        await update.message.reply_text(format_info(info), parse_mode=ParseMode.HTML)


def main() -> None:
    load_dotenv()  # loads .env if present

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit(
            "Missing TELEGRAM_TOKEN. Set it as an environment variable (or in .env)."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
