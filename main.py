"""
Course HTML Bot — v3 (VIDEO FORMAT + KOYEB FIX)
Apne khud ke course ki HTML file se Telegram bot.

v3 me kya fix hai (v2 se):
- Procfile me ab web: bhi hai, worker: bhi — Web Service ho ya Worker,
  dono me command milegi, "no command to run" error nahi aayega.
- Dockerfile bhi hai — Dockerfile builder se bhi chalega.
- Ek chhota health server bhi chalta hai (PORT par, default 8000) —
  Web Service ka health check pass ho jata hai, bot polling ke
  saath-saath chalta hai. Health server fail ho jaye to bhi bot
  rukta nahi, sirf warning log me aayegi.

Kaam:
- course.html ko parse karta hai (wahi structure: h1 title, .item,
  .title-btn = video link, .duration-badge, .pdf-btns = Notes / Slides)
- /start par course + lesson list dikhata hai
- Lesson par tap karo to ye button milte hain:
    📥 Video bhejo  = Telegram VIDEO FORMAT me bhejta hai (send_video)
    ▶️ Video kholo  = link browser me kholta hai
    📝 Notes / 📄 Slides = PDF link

VIDEO FORMAT ka rule (important):
- Sirf DIRECT video file link se video bheja ja sakta hai,
  jaise https://example.com/lesson-1.mp4
- Player page link (YouTube page, kisi player site ka page) se
  video FILE nahi nikalti — us par sirf "Video kholo" kaam karega.
  Aisa link chahiye to apni HTML me .mp4 wali direct link dalo.
- Bot kisi player page ko scrape / download nahi karta, koi login
  nahi karta, aur kisi aur ka paid video nahi nikalta.
  Sirf tumhare apne direct mp4 links ko Telegram video bana ke bhejta hai.
- Telegram Bot API limit: URL se bhejte time ~50 MB tak ka video
  aaram se jata hai; usse bada ho to "Video kholo" se kholo.

Apni HTML badalni ho to:
- Option A: repo me course.html replace karke redeploy karo
- Option B: bot ko HTML file bhej do (owner only) — bot use parse karke
  turant nayi list dikhane lagega

Env vars (Koyeb):
  BOT_TOKEN = BotFather wala token
  OWNER_ID  = tumhari Telegram numeric user id (optional, par reload ke liye recommended)
"""

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

VERSION = "v3"
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v")
BASE_DIR = Path(__file__).resolve().parent
COURSE_FILE = BASE_DIR / "course.html"
PAGE_SIZE = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("course-html-bot")

COURSE = {"title": "Course", "info": "", "lessons": []}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib name
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):  # noqa: ANN002, ANN202 - chup raho, log spam mat karo
        pass


def start_health_server() -> None:
    """Koyeb Web Service health check ke liye chhota HTTP server.

    Bot polling se alag thread me chalta hai, bot ko block nahi karta.
    Port busy ho ya fail ho jaye to sirf warning deke bot chalu rehta hai.
    """
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        port = 8000
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("HEALTH SERVER started on port %d", port)
    except Exception as e:  # noqa: BLE001 - health server optional hai
        logger.warning("HEALTH SERVER start nahi hua (bot phir bhi chalega): %s", e)


def parse_course_html(path: Path) -> dict:
    """Ussi HTML structure ko parse karo jo user ne di thi.

    Structure:
      <h1>COURSE TITLE</h1>
      <span class="info-badge">57 Lessons</span> ...
      <div class="item">
        <a class="title-btn" href="VIDEO_URL">Lesson title</a>
        <span class="duration-badge">1 hr 2 min</span>
        <div class="pdf-btns">
          <a class="pdf-btn" href="NOTES_URL">Notes</a>
          <a class="pdf-btn" href="SLIDES_URL">Slides</a>
        </div>
      </div>
    """
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title or "Course"

    info_parts = []
    for badge in soup.select(".info-badge"):
        text = badge.get_text(strip=True)
        if text:
            info_parts.append(text)
    info = " | ".join(info_parts)

    lessons = []
    items = soup.select(".item")
    # Fallback: agar .item na mile, to saare .title-btn ko order me lo
    if not items:
        for a in soup.select("a.title-btn"):
            lessons.append(
                {
                    "title": a.get_text(strip=True) or "Lesson",
                    "video_url": (a.get("href") or "").strip(),
                    "duration": "",
                    "notes_url": "",
                    "slides_url": "",
                }
            )
        return {"title": title, "info": info, "lessons": lessons}

    for item in items:
        title_btn = item.select_one("a.title-btn")
        if not title_btn:
            continue
        lesson_title = title_btn.get_text(strip=True) or "Lesson"
        video_url = (title_btn.get("href") or "").strip()

        duration = ""
        dur = item.select_one(".duration-badge")
        if dur:
            duration = dur.get_text(strip=True)

        notes_url = ""
        slides_url = ""
        for a in item.select(".pdf-btns a.pdf-btn, a.pdf-btn"):
            href = (a.get("href") or "").strip()
            label = a.get_text(strip=True).lower()
            if "note" in label and not notes_url:
                notes_url = href
            elif "slide" in label and not slides_url:
                slides_url = href
            elif not notes_url:
                notes_url = href
            elif not slides_url:
                slides_url = href

        lessons.append(
            {
                "title": lesson_title,
                "video_url": video_url,
                "duration": duration,
                "notes_url": notes_url,
                "slides_url": slides_url,
            }
        )

    return {"title": title, "info": info, "lessons": lessons}


def load_course() -> dict:
    global COURSE
    if not COURSE_FILE.exists():
        logger.warning("course.html nahi mili: %s", COURSE_FILE)
        COURSE = {"title": "Course", "info": "", "lessons": []}
        return COURSE
    COURSE = parse_course_html(COURSE_FILE)
    logger.info(
        "COURSE-BOT VERSION: %s | loaded %d lessons from %s",
        VERSION,
        len(COURSE["lessons"]),
        COURSE_FILE.name,
    )
    return COURSE


def is_direct_video_url(url: str) -> bool:
    """Direct video file link hai ya player page? Sirf direct file hi send_video se jati hai."""
    if not url:
        return False
    from urllib.parse import urlparse

    path = urlparse(url).path.lower()
    if path.endswith(VIDEO_EXTS):
        return True
    # query me .mp4 ho to bhi direct mana jata hai, jaise ?file=lesson.mp4
    return ".mp4" in url.lower() or ".mkv" in url.lower() or ".webm" in url.lower()


def is_owner(user_id: int) -> bool:
    owner = os.environ.get("OWNER_ID", "").strip()
    if not owner:
        return True  # OWNER_ID set nahi hai to koi bhi reload kar sakta hai (testing ke liye)
    return str(user_id) == owner


def start_text() -> str:
    total = len(COURSE["lessons"])
    lines = [f"<b>{COURSE['title']}</b>"]
    if COURSE["info"]:
        lines.append(COURSE["info"])
    lines.append(f"Total lessons: <b>{total}</b>")
    lines.append("")
    lines.append("Lesson dekhne ke liye neeche button dabao.")
    lines.append("Lesson kholo to '📥 Video bhejo' se Telegram video format me milega (direct .mp4 link ho to).")
    return "\n".join(lines)


def lessons_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    lessons = COURSE["lessons"]
    total_pages = max(1, (len(lessons) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = lessons[start : start + PAGE_SIZE]

    keyboard = []
    for idx, lesson in enumerate(chunk, start=start):
        label = lesson["title"]
        if len(label) > 60:
            label = label[:57] + "..."
        keyboard.append([InlineKeyboardButton(f"{idx + 1}. {label}", callback_data=f"lesson:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"list:{page + 1}"))
    if nav:
        keyboard.append(nav)
    return InlineKeyboardMarkup(keyboard)


def lesson_keyboard(idx: int) -> InlineKeyboardMarkup:
    lesson = COURSE["lessons"][idx]
    keyboard = []
    # VIDEO FORMAT: pehla button — Telegram video me bhejta hai
    if lesson["video_url"]:
        keyboard.append(
            [InlineKeyboardButton("📥 Video bhejo", callback_data=f"sendvideo:{idx}")]
        )
    row = []
    if lesson["video_url"]:
        row.append(InlineKeyboardButton("▶️ Video kholo", url=lesson["video_url"]))
    if row:
        keyboard.append(row)
    row = []
    if lesson["notes_url"]:
        row.append(InlineKeyboardButton("📝 Notes", url=lesson["notes_url"]))
    if lesson["slides_url"]:
        row.append(InlineKeyboardButton("📄 Slides", url=lesson["slides_url"]))
    if row:
        keyboard.append(row)
    page = idx // PAGE_SIZE
    keyboard.append([InlineKeyboardButton("⬅️ Lesson list", callback_data=f"list:{page}")])
    return InlineKeyboardMarkup(keyboard)


async def send_lesson_video(
    update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int
) -> None:
    """Lesson ko Telegram VIDEO FORMAT me bhejo (direct .mp4 link ho to)."""
    lesson = COURSE["lessons"][idx]
    url = lesson["video_url"]
    chat_id = update.effective_chat.id

    if not url:
        await context.bot.send_message(chat_id, "Is lesson me video link nahi hai.")
        return
    if not is_direct_video_url(url):
        await context.bot.send_message(
            chat_id,
            "Ye player page link hai, isse video file nahi banti.\n\n"
            "Video format me bhejne ke liye HTML me DIRECT video link chahiye, jaise:\n"
            "https://example.com/lesson-1.mp4\n\n"
            "Tab tak '▶️ Video kholo' se video khul jayegi.",
        )
        return

    caption = f"<b>{idx + 1}. {lesson['title']}</b>"
    if lesson["duration"]:
        caption += f"\n⏱️ {lesson['duration']}"
    try:
        await context.bot.send_video(
            chat_id=chat_id,
            video=url,
            caption=caption,
            parse_mode="HTML",
            supports_streaming=True,
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )
        logger.info("VIDEO SENT idx=%d url=%s", idx, url)
    except Exception as e:  # noqa: BLE001 - user ko fallback dikhana hai
        logger.warning("VIDEO SEND FAIL idx=%d err=%s", idx, e)
        await context.bot.send_message(
            chat_id,
            "Video bhejte time error aaya (bada file ya link issue ho sakta hai).\n"
            "'▶️ Video kholo' se khol ke dekho.\n"
            f"Link: {url}",
        )


async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/video 1 — pehli lesson video format me bhejo."""
    if not context.args:
        await update.message.reply_text(
            "Use karo: /video 1  (lesson number likho)\nExample: /video 1"
        )
        return
    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("Lesson number galat hai. Example: /video 1")
        return
    if idx < 0 or idx >= len(COURSE["lessons"]):
        await update.message.reply_text("Itni lesson nahi hai.")
        return
    await update.message.reply_text("Video bhej raha hun...")
    await send_lesson_video(update, context, idx)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        start_text(), reply_markup=lessons_keyboard(0), parse_mode="HTML"
    )


async def cmd_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"<b>{COURSE['title']}</b>\nTotal lessons: <b>{len(COURSE['lessons'])}</b>",
        reply_markup=lessons_keyboard(0),
        parse_mode="HTML",
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Ye command sirf owner ke liye hai.")
        return
    load_course()
    await update.message.reply_text(
        f"Reload ho gaya. Total lessons: {len(COURSE['lessons'])}"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("list:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await query.edit_message_text(
            f"<b>{COURSE['title']}</b>\nTotal lessons: <b>{len(COURSE['lessons'])}</b>",
            reply_markup=lessons_keyboard(page),
            parse_mode="HTML",
        )
        return

    if data.startswith("sendvideo:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        if idx < 0 or idx >= len(COURSE["lessons"]):
            await query.answer("Lesson nahi mili.", show_alert=True)
            return
        await query.answer("Video bhej raha hun...")
        await send_lesson_video(update, context, idx)
        return

    if data.startswith("lesson:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        if idx < 0 or idx >= len(COURSE["lessons"]):
            await query.answer("Lesson nahi mili.", show_alert=True)
            return
        lesson = COURSE["lessons"][idx]
        text = f"<b>{idx + 1}. {lesson['title']}</b>"
        if lesson["duration"]:
            text += f"\n⏱️ {lesson['duration']}"
        text += "\n\n'📥 Video bhejo' se Telegram video format me milega."
        await query.edit_message_text(
            text, reply_markup=lesson_keyboard(idx), parse_mode="HTML"
        )
        return


async def on_html_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner HTML file bhej de to use parse karke course update kar do."""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Ye sirf owner kar sakta hai.")
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".html"):
        await update.message.reply_text("Mujhe .html file bhejo.")
        return
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(COURSE_FILE)
    load_course()
    await update.message.reply_text(
        f"HTML update ho gayi.\nCourse: {COURSE['title']}\nTotal lessons: {len(COURSE['lessons'])}"
    )


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN env var set karo (Koyeb env me).")
    load_course()
    start_health_server()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CommandHandler("video", cmd_video))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.HTML, on_html_document))
    logger.info("COURSE-BOT VERSION: %s | polling start", VERSION)
    app.run_polling()


if __name__ == "__main__":
    main()
