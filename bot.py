"""
GurudevBook Official — Telegram Auto-Posting Bot
=================================================
Schedules pre-fill posts (30) over the first 3 days, then loops the
weekly calendar (28 posts) indefinitely. Posts at 10:00, 13:00, 18:00,
21:30 IST. Provides admin commands and keyword auto-replies.

Environment variables required:
  TELEGRAM_BOT_TOKEN  — your bot token from BotFather
  CHANNEL_USERNAME    — e.g. @gurudevbook_official  (default)
  TIMEZONE            — e.g. Asia/Kolkata            (default)
  REGISTER_URL        — e.g. https://gurudevbook.com/register (default)
  DATA_DIR            — writable dir for SQLite state (default: /app/data)
  CONTENT_DIR         — path to content/ folder     (default: /app/content)
  SMOKE_TEST_ON_BOOT  — set to "1" for first-boot test message
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ.get("CHANNEL_USERNAME", "@gurudevbook_official")
TZ_NAME = os.environ.get("TIMEZONE", "Asia/Kolkata")
TZ = pytz.timezone(TZ_NAME)
REGISTER_URL = os.environ.get("REGISTER_URL", "https://gurudevbook.com/register")

# Railway uses an ephemeral filesystem; we persist state in /app/data
# (committed to the repo so it survives redeploys via Railway Volumes if added)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "state.db"

# Content directory — baked into the repo
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", "/app/content"))
PREFILL_DIR = CONTENT_DIR / "prefill"
CALENDAR_DIR = CONTENT_DIR / "calendar"
CAPTIONS_FILE = CONTENT_DIR / "captions.json"

# Posts to auto-pin when first sent
PIN_FILES = {"26_warning_fake_channels.png", "30_founder_intro.png"}

# Pre-fill rollout plan: 12 / 12 / 6 across the 4 daily slots
SLOTS = [(10, 0), (13, 0), (18, 0), (21, 30)]
PREFILL_ROLLOUT_DAYS = 3
PREFILL_PER_DAY = [12, 12, 6]   # must sum to 30

# Daily calendar slot mapping based on filename suffix
SLOT_TIME_MAP = {
    "1000": (10, 0),
    "1300": (13, 0),
    "1800": (18, 0),
    "2130": (21, 30),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("gurudevbook")

# ---------------------------------------------------------------------------
# STATE (SQLite)
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sent_posts (
            file        TEXT PRIMARY KEY,
            sent_at     TEXT NOT NULL,
            message_id  INTEGER,
            cycle       INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS queue_extra (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at  TEXT NOT NULL,
            file    TEXT NOT NULL,
            caption TEXT NOT NULL,
            pin     INTEGER DEFAULT 0
        );
        """)

def setting_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def setting_set(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                  (key, value))

def is_admin(chat_id: int) -> bool:
    admin = setting_get("admin_chat_id")
    return admin is not None and str(chat_id) == admin

def mark_sent(file: str, message_id: int, cycle: int = 0):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO sent_posts(file,sent_at,message_id,cycle) "
                  "VALUES (?,?,?,?)",
                  (file, datetime.now(TZ).isoformat(), message_id, cycle))

def already_sent(file: str, cycle: int = 0) -> bool:
    with db() as c:
        row = c.execute("SELECT 1 FROM sent_posts WHERE file=? AND cycle=?",
                        (file, cycle)).fetchone()
    return row is not None

# ---------------------------------------------------------------------------
# CONTENT LOADER
# ---------------------------------------------------------------------------
def load_captions():
    """Returns ({prefill_file: caption}, {calendar_file: caption})."""
    if not CAPTIONS_FILE.exists():
        log.warning("captions.json missing — using empty captions")
        return {}, {}
    data = json.loads(CAPTIONS_FILE.read_text())
    pre = {p["file"]: p["caption"] for p in data.get("prefill", [])}
    cal = {c["file"]: c["caption"] for c in data.get("calendar", [])}
    return pre, cal

def list_prefill_in_order():
    """All 30 pre-fill PNGs in sorted (numeric) order."""
    files = sorted(p.name for p in PREFILL_DIR.glob("*.png"))
    return files

def pick_calendar_file(slot_hh_mm):
    hh, mm = slot_hh_mm
    suffix = f"{hh:02d}{mm:02d}"
    cycle_day = current_cycle_day()
    pattern = f"day{cycle_day:02d}_{suffix}_"
    for p in sorted(CALENDAR_DIR.glob(f"{pattern}*.png")):
        return p.name
    return None

def current_cycle_day() -> int:
    start_str = setting_get("calendar_start_date")
    if not start_str:
        return 1
    start = datetime.fromisoformat(start_str).date()
    today = datetime.now(TZ).date()
    days_in = (today - start).days
    return (days_in % 7) + 1

# ---------------------------------------------------------------------------
# PRE-FILL ROLLOUT
# ---------------------------------------------------------------------------
def prefill_schedule():
    files = list_prefill_in_order()
    now = datetime.now(TZ)
    today = now.date()
    schedule = []
    file_idx = 0

    day_idx = 0
    while file_idx < len(files) and day_idx < PREFILL_ROLLOUT_DAYS:
        target_date = today + timedelta(days=day_idx)
        per_day_quota = PREFILL_PER_DAY[day_idx]
        slot_pos = 0
        used_today = 0
        offset_minutes = 0
        while used_today < per_day_quota and file_idx < len(files):
            hh, mm = SLOTS[slot_pos % 4]
            ts = TZ.localize(datetime(target_date.year, target_date.month,
                                      target_date.day, hh, mm))
            ts = ts + timedelta(minutes=offset_minutes)
            if ts > now + timedelta(seconds=30):
                schedule.append((ts, files[file_idx]))
                file_idx += 1
                used_today += 1
            slot_pos += 1
            if slot_pos % 4 == 0:
                offset_minutes += 7
        day_idx += 1

    return schedule

# ---------------------------------------------------------------------------
# TELEGRAM POSTING
# ---------------------------------------------------------------------------
async def send_post(application, file_path: Path, caption: str,
                    pin: bool = False, log_label: str = ""):
    try:
        with open(file_path, "rb") as fh:
            msg = await application.bot.send_photo(
                chat_id=CHANNEL, photo=fh, caption=caption,
            )
        if pin:
            try:
                await application.bot.pin_chat_message(
                    chat_id=CHANNEL, message_id=msg.message_id,
                    disable_notification=True,
                )
                log.info("Pinned message %s", msg.message_id)
            except Exception as e:
                log.warning("Pin failed: %s", e)
        log.info("[POSTED %s] file=%s msg_id=%s",
                 log_label, file_path.name, msg.message_id)
        return msg.message_id
    except Exception as e:
        log.error("Failed to post %s: %s", file_path.name, e)
        return None

async def post_prefill_job(application, file_name: str):
    if already_sent(file_name, cycle=0):
        log.info("Skip %s — already sent", file_name)
        return
    if setting_get("paused") == "1":
        log.info("Paused — skipping prefill %s", file_name)
        return
    pre_caps, _ = load_captions()
    caption = pre_caps.get(file_name, f"GurudevBook Official — {file_name}")
    pin = file_name in PIN_FILES
    msg_id = await send_post(application, PREFILL_DIR / file_name, caption,
                             pin=pin, log_label="PREFILL")
    if msg_id:
        mark_sent(file_name, msg_id, cycle=0)

async def post_calendar_slot(application, slot_hh_mm):
    if setting_get("paused") == "1":
        log.info("Paused — skipping calendar slot %s", slot_hh_mm)
        return
    sent_count = 0
    with db() as c:
        sent_count = c.execute("SELECT COUNT(*) FROM sent_posts "
                               "WHERE cycle=0").fetchone()[0]
    if sent_count < len(list_prefill_in_order()):
        log.info("Pre-fill not finished (%d/30) — skipping calendar slot",
                 sent_count)
        return
    if not setting_get("calendar_start_date"):
        setting_set("calendar_start_date",
                    datetime.now(TZ).date().isoformat())
    file_name = pick_calendar_file(slot_hh_mm)
    if not file_name:
        log.warning("No calendar file for slot %s on day %d",
                    slot_hh_mm, current_cycle_day())
        return
    cycle = (datetime.now(TZ).date() -
             datetime.fromisoformat(setting_get("calendar_start_date"))
             .date()).days // 7 + 1
    if already_sent(file_name, cycle=cycle):
        log.info("Calendar %s already sent in cycle %d", file_name, cycle)
        return
    _, cal_caps = load_captions()
    caption = cal_caps.get(file_name, f"GurudevBook Official — {file_name}")
    msg_id = await send_post(application, CALENDAR_DIR / file_name, caption,
                             pin=False, log_label=f"CAL D{current_cycle_day()}")
    if msg_id:
        mark_sent(file_name, msg_id, cycle=cycle)

# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------
HELP_TEXT = (
    "*GurudevBook Bot — Admin Commands*\n\n"
    "/claim — claim admin (first user wins)\n"
    "/status — next 5 scheduled posts\n"
    "/list — list upcoming queue\n"
    "/skip — skip the next scheduled post\n"
    "/pause — pause auto-posting\n"
    "/resume — resume auto-posting\n"
    "/help — this menu"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to GurudevBook VIP Support! \n\n"
        f"100% welcome bonus claim karne ke liye yahan register karein:\n"
        f"{REGISTER_URL}\n\n"
        "Type 'tips', 'casino', 'bonus' or 'support' for quick info.\n"
        "Admins: send /claim from your personal chat to gain control."
    )

async def cmd_claim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    existing = setting_get("admin_chat_id")
    chat_id = update.effective_chat.id
    if existing:
        if str(chat_id) == existing:
            await update.message.reply_text("You are already admin")
        else:
            await update.message.reply_text(
                "Admin already claimed. Contact existing admin.")
        return
    setting_set("admin_chat_id", str(chat_id))
    await update.message.reply_text(
        f"Admin claimed! Your chat ID {chat_id} is now the bot owner.\n\n"
        "Send /help to see admin commands.")
    log.info("Admin claimed by chat_id=%s", chat_id)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    sched: AsyncIOScheduler = ctx.application.bot_data["scheduler"]
    jobs = sorted(sched.get_jobs(),
                  key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=TZ))
    paused = setting_get("paused") == "1"
    lines = ["PAUSED" if paused else "Active",
             f"Total scheduled jobs: {len(jobs)}",
             "Next 5:"]
    for j in jobs[:5]:
        nrt = j.next_run_time
        lines.append(f"  {nrt.astimezone(TZ).strftime('%a %d %b %H:%M')} — {j.id}")
    await update.message.reply_text("\n".join(lines))

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    sched: AsyncIOScheduler = ctx.application.bot_data["scheduler"]
    jobs = sorted(sched.get_jobs(),
                  key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=TZ))
    if not jobs:
        await update.message.reply_text("Queue empty.")
        return
    lines = [f"Upcoming ({len(jobs)}):"]
    for j in jobs[:20]:
        nrt = j.next_run_time
        lines.append(f"  {nrt.astimezone(TZ).strftime('%d %b %H:%M')} — {j.id}")
    await update.message.reply_text("\n".join(lines))

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    sched: AsyncIOScheduler = ctx.application.bot_data["scheduler"]
    jobs = sorted([j for j in sched.get_jobs() if j.next_run_time],
                  key=lambda j: j.next_run_time)
    if not jobs:
        await update.message.reply_text("No upcoming jobs to skip.")
        return
    j = jobs[0]
    sched.remove_job(j.id)
    await update.message.reply_text(f"Skipped: {j.id}")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    setting_set("paused", "1")
    await update.message.reply_text("Auto-posting paused.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    setting_set("paused", "0")
    await update.message.reply_text("Auto-posting resumed.")

# ---------------------------------------------------------------------------
# AUTO-REPLIES (DM)
# ---------------------------------------------------------------------------
AUTO_REPLIES = {
    "tips": (f"Aaj ke VIP tips hamare official channel pe post ho chuke hain. "
             f"Check karo: {CHANNEL}"),
    "match": (f"Aaj ke VIP tips hamare official channel pe post ho chuke hain. "
              f"Check karo: {CHANNEL}"),
    "casino": (f"Live casino tables open hain! Aviator, Roulette aur bahut kuch. "
               f"Apni ID login karo aur khelo: {REGISTER_URL}"),
    "games": (f"Live casino tables open hain! Aviator, Roulette aur bahut kuch. "
              f"Apni ID login karo aur khelo: {REGISTER_URL}"),
    "bonus": (f"100% Welcome Bonus claim karne ke liye abhi register karein: "
              f"{REGISTER_URL}\nRegistration ke baad apna username yahan bhejein."),
    "register": (f"100% Welcome Bonus claim karne ke liye abhi register karein: "
                 f"{REGISTER_URL}\nRegistration ke baad apna username yahan bhejein."),
    "id": (f"100% Welcome Bonus claim karne ke liye abhi register karein: "
           f"{REGISTER_URL}"),
    "vip": (f"VIP club me judne ke liye pehla deposit complete karein: "
            f"{REGISTER_URL}\nUske baad aapko premium tips milna shuru ho jayenge."),
    "support": ("Hamari team 24x7 available hai. Kripya apna sawal yahan type "
                "karein, ek agent jaldi hi aapse connect karega."),
    "help": ("Hamari team 24x7 available hai. Kripya apna sawal yahan type "
             "karein, ek agent jaldi hi aapse connect karega."),
}

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.effective_chat.type != "private":
        return
    text = update.message.text.lower().strip()
    for keyword, reply in AUTO_REPLIES.items():
        if keyword in text:
            await update.message.reply_text(reply, disable_web_page_preview=True)
            return
    if text in {"hi", "hello", "hey", "namaste"}:
        await update.message.reply_text(
            "Welcome to GurudevBook! \nType 'tips', 'casino', 'bonus' or "
            "'support' for instant info, or visit our channel: " + CHANNEL)

# ---------------------------------------------------------------------------
# SCHEDULER WIRING
# ---------------------------------------------------------------------------
def build_scheduler(application) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=TZ)

    # 1) Schedule pre-fill rollout (one-shot dated jobs)
    plan = prefill_schedule()
    for ts, file_name in plan:
        if already_sent(file_name, cycle=0):
            continue
        job_id = f"prefill::{file_name}"
        sched.add_job(post_prefill_job, "date", run_date=ts,
                      args=[application, file_name], id=job_id,
                      replace_existing=True, misfire_grace_time=3600)
        log.info("Scheduled pre-fill %s at %s", file_name,
                 ts.strftime("%a %d %b %H:%M %Z"))

    # 2) Schedule the 4 daily calendar slots (recurring cron)
    for hh, mm in SLOTS:
        job_id = f"cal::{hh:02d}{mm:02d}"
        sched.add_job(post_calendar_slot, CronTrigger(hour=hh, minute=mm,
                                                       timezone=TZ),
                      args=[application, (hh, mm)], id=job_id,
                      replace_existing=True, misfire_grace_time=600)
        log.info("Scheduled daily calendar slot %02d:%02d IST", hh, mm)

    return sched

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
async def post_init(application):
    init_db()
    sched = build_scheduler(application)
    sched.start()
    application.bot_data["scheduler"] = sched

    if os.environ.get("SMOKE_TEST_ON_BOOT") == "1" and \
       setting_get("smoke_done") != "1":
        try:
            now_str = datetime.now(TZ).strftime("%H:%M IST on %d %b %Y")
            msg = await application.bot.send_message(
                chat_id=CHANNEL,
                text=(f"GurudevBook auto-poster is LIVE.\n"
                      f"First scheduled post will go out at the next slot "
                      f"(10:00 / 13:00 / 18:00 / 21:30 IST).\n"
                      f"Boot time: {now_str}"))
            setting_set("smoke_done", "1")
            log.info("Smoke test message sent (msg_id=%s). "
                     "You can delete it from the channel manually.", msg.message_id)
        except Exception as e:
            log.error("Smoke test failed: %s", e)

def main():
    application = (Application.builder()
                   .token(TOKEN)
                   .post_init(post_init)
                   .build())

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("claim", cmd_claim))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("skip", cmd_skip))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           on_text))

    log.info("GurudevBook bot starting. Channel=%s TZ=%s", CHANNEL, TZ_NAME)
    application.run_polling(drop_pending_updates=True,
                            allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
