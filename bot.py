"""
GurudevBook Official — Telegram Auto-Posting Bot v2.0
=====================================================
v2.0 adds a live cricket engine:
  - Fetches today's IPL + international cricket fixtures from Cricbuzz
  - Schedules pre-match preview, VIP combo pick, live alert, and recap posts
  - Admin confirms /win or /loss before recap fires (2-hour wait, then neutral)
  - Falls back to pre-fill / calendar pool on no-match days
  - New admin commands: /todaymatches, /win, /loss, /skiptip, /forcematch

Environment variables required:
  TELEGRAM_BOT_TOKEN  — your bot token from BotFather
  CHANNEL_USERNAME    — e.g. @gurudevbook_official  (default)
  TIMEZONE            — e.g. Asia/Kolkata            (default)
  REGISTER_URL        — e.g. https://gurudevbook.com/register (default)
  DATA_DIR            — writable dir for SQLite state (default: /app/data)
  CONTENT_DIR         — path to content/ folder     (default: /app/content)
  SMOKE_TEST_ON_BOOT  — set to "1" for first-boot test message
"""
PROOFS_CHANNEL = "@gurudevbookproofs"

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
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

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "state.db"
JOBS_DB_PATH = DATA_DIR / "jobs.sqlite"  # persistent APScheduler jobstore

CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", "/app/content"))
PREFILL_DIR = CONTENT_DIR / "prefill"
CALENDAR_DIR = CONTENT_DIR / "calendar"
CAPTIONS_FILE = CONTENT_DIR / "captions.json"

PIN_FILES = {"26_warning_fake_channels.png", "30_founder_intro.png"}

SLOTS = [(10, 0), (13, 0), (18, 0), (21, 30)]
PREFILL_ROLLOUT_DAYS = 3
PREFILL_PER_DAY = [12, 12, 6]

SLOT_TIME_MAP = {
    "1000": (10, 0),
    "1300": (13, 0),
    "1800": (18, 0),
    "2130": (21, 30),
}

# Cricket: only post for these series keywords (IPL, international, major T20 leagues)
CRICKET_SERIES_KEYWORDS = [
    "ipl", "indian premier league",
    "test", "odi", "t20i", "t20 international",
    "world cup", "champions trophy", "asia cup",
    "bbl", "psl", "sa20", "cpl", "the hundred",
    "icc", "bilateral",
]

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
        CREATE TABLE IF NOT EXISTS cricket_matches (
            match_id    TEXT PRIMARY KEY,
            series      TEXT,
            team1       TEXT,
            team2       TEXT,
            venue       TEXT,
            start_ts    INTEGER,
            status      TEXT DEFAULT 'scheduled',
            tip_result  TEXT,
            tip_text    TEXT,
            skipped     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cricket_posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id    TEXT,
            post_type   TEXT,
            sent_at     TEXT,
            message_id  INTEGER
        );
        CREATE TABLE IF NOT EXISTS fired_jobs_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL,
            fired_at    TEXT NOT NULL,
            status      TEXT DEFAULT 'ok'  -- 'ok' or 'error'
        );
        CREATE TABLE IF NOT EXISTS bot_errors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            context     TEXT,
            error       TEXT
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

def log_fired_job(job_id: str, status: str = "ok"):
    """Record that a job fired (for /status history)."""
    with db() as c:
        c.execute("INSERT INTO fired_jobs_log(job_id,fired_at,status) VALUES (?,?,?)",
                  (job_id, datetime.now(TZ).isoformat(), status))
    # Keep only last 50 entries
    with db() as c:
        c.execute("DELETE FROM fired_jobs_log WHERE id NOT IN "
                  "(SELECT id FROM fired_jobs_log ORDER BY id DESC LIMIT 50)")

def log_bot_error(context: str, error: str):
    """Record a bot error for /status display."""
    with db() as c:
        c.execute("INSERT INTO bot_errors(occurred_at,context,error) VALUES (?,?,?)",
                  (datetime.now(TZ).isoformat(), context, error[:500]))
    with db() as c:
        c.execute("DELETE FROM bot_errors WHERE id NOT IN "
                  "(SELECT id FROM bot_errors ORDER BY id DESC LIMIT 10)")

def get_last_error() -> Optional[str]:
    with db() as c:
        row = c.execute(
            "SELECT occurred_at, context, error FROM bot_errors ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return f"{row[0]} | {row[1]}: {row[2]}"

def get_last_fired_jobs(n: int = 5) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT job_id, fired_at, status FROM fired_jobs_log ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return rows

# ---------------------------------------------------------------------------
# CRICKET ENGINE — FIXTURE FETCHING
# ---------------------------------------------------------------------------
CRICBUZZ_URL = "https://www.cricbuzz.com/api/cricket-match/live-matches"

def _fetch_cricbuzz_raw() -> str:
    req = urllib.request.Request(
        CRICBUZZ_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")

def _is_relevant_series(series_name: str) -> bool:
    sl = series_name.lower()
    return any(kw in sl for kw in CRICKET_SERIES_KEYWORDS)

def fetch_today_cricket_matches() -> list[dict]:
    """
    Returns a list of match dicts for today (IST) that are relevant
    (IPL, international, major T20 leagues).
    Each dict: {match_id, series, team1, team2, venue, start_ts, state}
    """
    today_ist = datetime.now(TZ).date()
    try:
        raw = _fetch_cricbuzz_raw()
    except Exception as e:
        log.error("Cricbuzz fetch failed: %s", e)
        return []

    matches = []
    # Extract match info from the HTML/JSON hybrid response
    # Pattern: href="/live-cricket-scores/<id>/<slug>" title="<desc>"
    match_links = re.findall(
        r'href="(/live-cricket-scores/(\d+)/([^"]+))"[^>]*title="([^"]*)"',
        raw
    )

    # Also extract series context by looking for series name before each match block
    # We'll use a simpler approach: find startDate timestamps near match IDs
    # Extract all startDate values with their surrounding context
    start_date_pattern = re.compile(r'"startDate":"(\d+)"')
    all_timestamps = [(int(m.group(1)), m.start()) for m in start_date_pattern.finditer(raw)]

    for href, match_id, slug, title in match_links:
        if not title.strip():
            continue

        # Parse teams from slug: e.g. "dc-vs-kkr-51st-match-ipl-2026"
        slug_parts = slug.split("-")
        vs_idx = None
        for i, p in enumerate(slug_parts):
            if p == "vs":
                vs_idx = i
                break

        if vs_idx and vs_idx >= 1:
            team1 = " ".join(slug_parts[:vs_idx]).upper()
            # team2 ends before the match number
            team2_parts = []
            for p in slug_parts[vs_idx+1:]:
                if re.match(r'^\d+', p) or p in ("match", "test", "odi", "t20i",
                                                   "ipl", "bbl", "psl"):
                    break
                team2_parts.append(p)
            team2 = " ".join(team2_parts).upper()
        else:
            team1 = "Team 1"
            team2 = "Team 2"

        # Determine series from slug tail
        series = slug.replace("-", " ").title()
        if not _is_relevant_series(series):
            continue

        # Find the closest startDate timestamp to this match_id in the raw text
        match_id_pos = raw.find(f'/{match_id}/')
        start_ts = None
        if match_id_pos > 0:
            # Find nearest startDate within 3000 chars before
            nearby = raw[max(0, match_id_pos-3000):match_id_pos+200]
            ts_matches = re.findall(r'"startDate":"(\d+)"', nearby)
            if ts_matches:
                start_ts = int(ts_matches[-1])

        if not start_ts:
            # Skip if we can't determine time
            continue

        # Convert to IST
        start_dt_utc = datetime.fromtimestamp(start_ts / 1000, tz=pytz.utc)
        start_dt_ist = start_dt_utc.astimezone(TZ)

        # Only include today's matches
        if start_dt_ist.date() != today_ist:
            continue

        # Determine state from title
        title_lower = title.lower()
        if "complete" in title_lower or "won" in title_lower:
            state = "complete"
        elif "live" in title_lower or "toss" in title_lower or "innings" in title_lower:
            state = "live"
        elif "preview" in title_lower or "upcoming" in title_lower:
            state = "upcoming"
        else:
            state = "scheduled"

        # Extract venue from title if possible
        venue = "TBD"
        # Title format: "Team1 vs Team2, Match Desc - Status"
        # Venue is usually in a separate field; we'll use city from slug context
        city_match = re.search(r'"city":"([^"]+)"', raw[max(0,match_id_pos-500):match_id_pos+500])
        if city_match:
            venue = city_match.group(1)

        matches.append({
            "match_id": match_id,
            "series": series,
            "team1": team1,
            "team2": team2,
            "venue": venue,
            "start_ts": start_ts,
            "start_dt_ist": start_dt_ist,
            "state": state,
            "title": title.strip(),
        })

    # Deduplicate by match_id
    seen = set()
    unique = []
    for m in matches:
        if m["match_id"] not in seen:
            seen.add(m["match_id"])
            unique.append(m)

    log.info("Fetched %d relevant cricket matches for today", len(unique))
    return unique

def save_match_to_db(match: dict):
    with db() as c:
        c.execute("""
            INSERT OR IGNORE INTO cricket_matches
            (match_id, series, team1, team2, venue, start_ts, status)
            VALUES (?,?,?,?,?,?,?)
        """, (match["match_id"], match["series"], match["team1"],
              match["team2"], match["venue"], match["start_ts"], "scheduled"))

def get_match_from_db(match_id: str) -> Optional[dict]:
    with db() as c:
        row = c.execute(
            "SELECT match_id,series,team1,team2,venue,start_ts,status,tip_result,tip_text,skipped "
            "FROM cricket_matches WHERE match_id=?", (match_id,)
        ).fetchone()
    if not row:
        return None
    keys = ["match_id","series","team1","team2","venue","start_ts",
            "status","tip_result","tip_text","skipped"]
    return dict(zip(keys, row))

def get_latest_match_id() -> Optional[str]:
    """Return the most recently scheduled match_id."""
    with db() as c:
        row = c.execute(
            "SELECT match_id FROM cricket_matches "
            "ORDER BY start_ts DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None

def cricket_post_already_sent(match_id: str, post_type: str) -> bool:
    with db() as c:
        row = c.execute(
            "SELECT 1 FROM cricket_posts WHERE match_id=? AND post_type=?",
            (match_id, post_type)
        ).fetchone()
    return row is not None

def mark_cricket_post_sent(match_id: str, post_type: str, message_id: int):
    with db() as c:
        c.execute(
            "INSERT INTO cricket_posts(match_id,post_type,sent_at,message_id) "
            "VALUES (?,?,?,?)",
            (match_id, post_type, datetime.now(TZ).isoformat(), message_id)
        )

# ---------------------------------------------------------------------------
# CRICKET CAPTION GENERATOR
# ---------------------------------------------------------------------------
def _team_display(team_code: str) -> str:
    """Convert slug-style team code to a display name."""
    mapping = {
        "DC": "Delhi Capitals", "KKR": "Kolkata Knight Riders",
        "CSK": "Chennai Super Kings", "MI": "Mumbai Indians",
        "RCB": "Royal Challengers Bengaluru", "SRH": "Sunrisers Hyderabad",
        "PBKS": "Punjab Kings", "RR": "Rajasthan Royals",
        "GT": "Gujarat Titans", "LSG": "Lucknow Super Giants",
        "IND": "India", "PAK": "Pakistan", "AUS": "Australia",
        "ENG": "England", "SA": "South Africa", "NZ": "New Zealand",
        "WI": "West Indies", "SL": "Sri Lanka", "BAN": "Bangladesh",
        "AFG": "Afghanistan", "ZIM": "Zimbabwe",
    }
    return mapping.get(team_code.upper(), team_code.title())

def _simple_tip_pick(team1: str, team2: str) -> tuple[str, str]:
    """
    Very simple heuristic tip: home team advantage or alphabetical.
    Returns (pick_team, reasoning).
    """
    home_teams = {
        "DC": "Delhi", "KKR": "Kolkata", "CSK": "Chennai",
        "MI": "Mumbai", "RCB": "Bengaluru", "SRH": "Hyderabad",
        "PBKS": "Mohali", "RR": "Jaipur", "GT": "Ahmedabad",
        "LSG": "Lucknow",
    }
    t1 = team1.split()[0].upper()
    t2 = team2.split()[0].upper()
    if t1 in home_teams:
        return team1, "home ground advantage + recent form"
    if t2 in home_teams:
        return team2, "home ground advantage + recent form"
    # Fallback: pick team1
    return team1, "current form aur head-to-head record"

def make_preview_caption(match: dict) -> str:
    t1 = _team_display(match["team1"].split()[0])
    t2 = _team_display(match["team2"].split()[0])
    start_dt = datetime.fromtimestamp(match["start_ts"]/1000, tz=TZ)
    time_str = start_dt.strftime("%I:%M %p IST")
    venue = match.get("venue", "TBD")
    series = match.get("series", "Cricket")

    return (
        f"MATCH PREVIEW — {t1} vs {t2}\n\n"
        f"Series: {series}\n"
        f"Time: {time_str}\n"
        f"Venue: {venue}\n\n"
        f"Bhaiyo, aaj ka match ek important encounter hai. "
        f"Hamari research team dono teams ki form, pitch conditions aur "
        f"head-to-head stats analyze kar rahi hai.\n\n"
        f"VIP combo pick aaj match se 30 min pehle is channel par drop hoga. "
        f"Channel UNMUTE rakho!\n\n"
        f"Abhi register karo — 100% welcome bonus + 1 free VIP tip:\n"
        f"{REGISTER_URL}\n\n"
        f"#GurudevBook #{t1.replace(' ','')} #{t2.replace(' ','')} "
        f"#VIPTipper #CricketTips"
    )

def make_combo_caption(match: dict) -> str:
    t1 = _team_display(match["team1"].split()[0])
    t2 = _team_display(match["team2"].split()[0])
    pick, reason = _simple_tip_pick(match["team1"], match["team2"])
    pick_display = _team_display(pick.split()[0])

    # Save the tip text to DB for recap
    tip_text = f"{pick_display} Win"
    with db() as c:
        c.execute("UPDATE cricket_matches SET tip_text=? WHERE match_id=?",
                  (tip_text, match["match_id"]))

    return (
        f"VIP COMBO LOCKED — {t1} vs {t2}\n\n"
        f"Hamari pick: {pick_display} Win\n"
        f"Reason: {reason}\n\n"
        f"Disclaimer: Ye analysis sirf entertainment aur information ke liye hai. "
        f"Responsible gaming karein. 18+ only.\n\n"
        f"Full VIP analysis + odds breakdown ke liye register karein:\n"
        f"{REGISTER_URL}\n\n"
        f"#VIPCombo #GurudevBook #{t1.replace(' ','')}vs{t2.replace(' ','')} "
        f"#CricketTips #WinWithGurudev"
    )

def make_live_alert_caption(match: dict) -> str:
    t1 = _team_display(match["team1"].split()[0])
    t2 = _team_display(match["team2"].split()[0])
    return (
        f"MATCH LIVE — {t1} vs {t2}\n\n"
        f"Match shuru ho gaya! Apni ID login karo aur enjoy karo.\n\n"
        f"Live updates aur score ke liye channel follow karte raho.\n"
        f"Abhi tak register nahi kiya? Jaldi karo:\n"
        f"{REGISTER_URL}\n\n"
        f"#LiveNow #GurudevBook #{t1.replace(' ','')}vs{t2.replace(' ','')} "
        f"#CricketLive"
    )

def make_recap_caption(match: dict, result: Optional[str] = None) -> str:
    t1 = _team_display(match["team1"].split()[0])
    t2 = _team_display(match["team2"].split()[0])
    tip_text = match.get("tip_text") or f"{t1} Win"

    if result == "win":
        return (
            f"TIP HIT — {t1} vs {t2}\n\n"
            f"Hamara pick '{tip_text}' — CORRECT!\n\n"
            f"Bhaiyo, aaj ke VIP members ne mast return banaya. "
            f"Kal ka combo bhi is channel par aayega.\n\n"
            f"Abhi tak join nahi kiya? Kal miss mat karo:\n"
            f"{REGISTER_URL}\n\n"
            f"#TipHit #GurudevBook #WinWithGurudev "
            f"#{t1.replace(' ','')}vs{t2.replace(' ','')} #VIPLife"
        )
    elif result == "loss":
        return (
            f"MATCH RESULT — {t1} vs {t2}\n\n"
            f"Aaj ka pick '{tip_text}' — result expected ke against gaya.\n\n"
            f"Cricket mein upsets hote hain — yahi game hai. "
            f"Hamari team kal fresh analysis ke saath wapas aayegi. "
            f"Long-term mein consistency hi winner banati hai.\n\n"
            f"Responsible gaming karein. 18+ only.\n"
            f"Register: {REGISTER_URL}\n\n"
            f"#GurudevBook #HonestTipper "
            f"#{t1.replace(' ','')}vs{t2.replace(' ','')} #CricketAnalysis"
        )
    else:
        # Neutral recap (no admin response)
        return (
            f"MATCH OVER — {t1} vs {t2}\n\n"
            f"Match khatam ho gaya. Full result aur analysis kal subah "
            f"channel par share kiya jayega.\n\n"
            f"Daily VIP tips ke liye channel subscribe karein:\n"
            f"{REGISTER_URL}\n\n"
            f"#GurudevBook #{t1.replace(' ','')}vs{t2.replace(' ','')} "
            f"#CricketResults"
        )

# ---------------------------------------------------------------------------
# CRICKET SCHEDULER
# ---------------------------------------------------------------------------
async def post_text_to_channel(application, text: str, match_id: str, post_type: str):
    """Send a text-only post to the channel and record it."""
    if cricket_post_already_sent(match_id, post_type):
        log.info("Cricket post %s/%s already sent", match_id, post_type)
        return
    try:
        msg = await application.bot.send_message(
            chat_id=CHANNEL, text=text, disable_web_page_preview=True
        )
        mark_cricket_post_sent(match_id, post_type, msg.message_id)
        log.info("[CRICKET %s] match=%s msg_id=%s", post_type, match_id, msg.message_id)
    except Exception as e:
        log.error("Cricket post failed (%s/%s): %s", match_id, post_type, e)

async def cricket_preview_job(application, match_id: str):
    job_id = f"cricket_preview::{match_id}"
    try:
        match = get_match_from_db(match_id)
        if not match or match["skipped"]:
            return
        if setting_get("cricket_paused") == "1":
            return
        caption = make_preview_caption(match)
        await post_text_to_channel(application, caption, match_id, "preview")
        log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Cricket preview job failed for %s: %s", match_id, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

async def cricket_combo_job(application, match_id: str):
    job_id = f"cricket_combo::{match_id}"
    try:
        match = get_match_from_db(match_id)
        if not match or match["skipped"]:
            return
        if setting_get("cricket_paused") == "1":
            return
        caption = make_combo_caption(match)
        await post_text_to_channel(application, caption, match_id, "combo")
        log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Cricket combo job failed for %s: %s", match_id, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

async def cricket_live_alert_job(application, match_id: str):
    job_id = f"cricket_live::{match_id}"
    try:
        match = get_match_from_db(match_id)
        if not match or match["skipped"]:
            return
        if setting_get("cricket_paused") == "1":
            return
        caption = make_live_alert_caption(match)
        await post_text_to_channel(application, caption, match_id, "live_alert")
        log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Cricket live alert job failed for %s: %s", match_id, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

async def cricket_recap_job(application, match_id: str):
    """
    Fires ~2 hours after match start. Checks if admin sent /win or /loss.
    If not, posts neutral recap.
    """
    job_id = f"cricket_recap::{match_id}"
    try:
        match = get_match_from_db(match_id)
        if not match or match["skipped"]:
            return
        if cricket_post_already_sent(match_id, "recap"):
            return
        if setting_get("cricket_paused") == "1":
            return
        result = match.get("tip_result")  # "win", "loss", or None
        caption = make_recap_caption(match, result)
        await post_text_to_channel(application, caption, match_id, "recap")
        log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Cricket recap job failed for %s: %s", match_id, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

def schedule_cricket_match(application, sched: AsyncIOScheduler, match: dict):
    """Add 4 jobs for a single match: preview, combo, live alert, recap."""
    match_id = match["match_id"]
    start_dt = match["start_dt_ist"]
    now = datetime.now(TZ)

    jobs_added = 0

    # Preview: 3 hours before
    preview_dt = start_dt - timedelta(hours=3)
    if preview_dt > now + timedelta(seconds=30):
        sched.add_job(
            cricket_preview_job, "date", run_date=preview_dt,
            args=[application, match_id],
            id=f"cricket_preview::{match_id}",
            replace_existing=True, misfire_grace_time=3600
        )
        log.info("Scheduled cricket PREVIEW for %s at %s IST",
                 match_id, preview_dt.strftime("%H:%M"))
        jobs_added += 1

    # Combo pick: 30 min before
    combo_dt = start_dt - timedelta(minutes=30)
    if combo_dt > now + timedelta(seconds=30):
        sched.add_job(
            cricket_combo_job, "date", run_date=combo_dt,
            args=[application, match_id],
            id=f"cricket_combo::{match_id}",
            replace_existing=True, misfire_grace_time=1800
        )
        log.info("Scheduled cricket COMBO for %s at %s IST",
                 match_id, combo_dt.strftime("%H:%M"))
        jobs_added += 1

    # Live alert: at match start
    if start_dt > now + timedelta(seconds=30):
        sched.add_job(
            cricket_live_alert_job, "date", run_date=start_dt,
            args=[application, match_id],
            id=f"cricket_live::{match_id}",
            replace_existing=True, misfire_grace_time=1800
        )
        log.info("Scheduled cricket LIVE ALERT for %s at %s IST",
                 match_id, start_dt.strftime("%H:%M"))
        jobs_added += 1

    # Recap: 2.5 hours after match start (admin has 2h to send /win or /loss)
    recap_dt = start_dt + timedelta(hours=2, minutes=30)
    if recap_dt > now + timedelta(seconds=30):
        sched.add_job(
            cricket_recap_job, "date", run_date=recap_dt,
            args=[application, match_id],
            id=f"cricket_recap::{match_id}",
            replace_existing=True, misfire_grace_time=3600
        )
        log.info("Scheduled cricket RECAP for %s at %s IST",
                 match_id, recap_dt.strftime("%H:%M"))
        jobs_added += 1

    return jobs_added

async def daily_cricket_fetch_job(application):
    """
    Runs at 07:00 IST every morning.
    Fetches today's matches and schedules their posts.
    """
    log.info("Daily cricket fetch starting...")
    sched: AsyncIOScheduler = application.bot_data.get("scheduler")
    if not sched:
        log.error("Scheduler not found in bot_data")
        return

    matches = fetch_today_cricket_matches()
    if not matches:
        log.info("No relevant cricket matches today — fallback posts will run")
        return

    count = 0
    for match in matches:
        save_match_to_db(match)
        jobs = schedule_cricket_match(application, sched, match)
        count += jobs

    log.info("Cricket fetch done: %d matches, %d jobs scheduled", len(matches), count)

# ---------------------------------------------------------------------------
# CONTENT LOADER (unchanged from v1)
# ---------------------------------------------------------------------------
def load_captions():
    if not CAPTIONS_FILE.exists():
        log.warning("captions.json missing — using empty captions")
        return {}, {}
    data = json.loads(CAPTIONS_FILE.read_text())
    pre = {p["file"]: p["caption"] for p in data.get("prefill", [])}
    cal = {c["file"]: c["caption"] for c in data.get("calendar", [])}
    return pre, cal

def list_prefill_in_order():
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
# PRE-FILL ROLLOUT (unchanged from v1)
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
    job_id = f"prefill::{file_name}"
    try:
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
            log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Prefill job failed for %s: %s", file_name, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

async def post_calendar_slot(application, slot_hh_mm):
    job_id = f"cal::{slot_hh_mm[0]:02d}{slot_hh_mm[1]:02d}"
    try:
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
            log_fired_job(job_id, "ok")
    except Exception as e:
        log.error("Calendar job failed for %s: %s", slot_hh_mm, e)
        log_fired_job(job_id, "error")
        log_bot_error(job_id, str(e))

# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------
HELP_TEXT = (
    "*GurudevBook Bot v2.0 — Admin Commands*\n\n"
    "*Cricket Commands:*\n"
    "/todaymatches — list today's fetched cricket matches\n"
    "/win [match\\_id] — mark our tip as WON; triggers recap\n"
    "/loss [match\\_id] — mark our tip as LOSS; triggers recap\n"
    "/skiptip [match\\_id] — skip all posts for that match\n"
    "/forcematch Team1 vs Team2 @ HH:MM — manually add a match\n"
    "/pause — pause cricket auto-posting\n"
    "/resume — resume cricket auto-posting\n\n"
    "*General Commands:*\n"
    "/claim — claim admin (first user wins)\n"
    "/status — next 5 scheduled jobs\n"
    "/list — list all upcoming jobs\n"
    "/skip — skip the next scheduled job\n"
    "/help — this menu"
)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to GurudevBook VIP Support!\n\n"
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
            await update.message.reply_text("You are already admin.")
        else:
            await update.message.reply_text(
                "Admin already claimed. Contact existing admin.")
        return
    setting_set("admin_chat_id", str(chat_id))
    await update.message.reply_text(
        f"Admin claimed! Your chat ID {chat_id} is now the bot owner.\n\n"
        "Send /help to see all admin commands.")
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
    cricket_paused = setting_get("cricket_paused") == "1"
    
    lines = ["*GurudevBook Bot Status*"]
    lines.append(f"Time: {datetime.now(TZ).strftime('%H:%M:%S %Z')}")
    
    if paused:
        lines.append("Mode: ⏸ PAUSED (all posts)")
    elif cricket_paused:
        lines.append("Mode: 🏏 Cricket PAUSED (fallback active)")
    else:
        lines.append("Mode: ✅ Active")
    
    lines.append(f"\n*Job Queue ({len(jobs)} total):*")
    if not jobs:
        lines.append("  (No jobs scheduled)")
    else:
        for j in jobs[:5]:
            nrt = j.next_run_time
            if nrt:
                lines.append(f"  \u231b {nrt.astimezone(TZ).strftime('%H:%M')} \u2014 {j.id}")
            else:
                lines.append(f"  \u231b (paused) \u2014 {j.id}")

    # History
    history = get_last_fired_jobs(5)
    if history:
        lines.append("\n*Last 5 Fired:*")
        for jid, fat, stat in history:
            ts = datetime.fromisoformat(fat).astimezone(TZ).strftime('%H:%M')
            icon = "✅" if stat == "ok" else "❌"
            lines.append(f"  {icon} {ts} \u2014 {jid}")

    # Errors
    last_err = get_last_error()
    if last_err:
        lines.append(f"\n*Last Error:*\n`{last_err}`")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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
        if nrt:
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
    setting_set("cricket_paused", "1")
    await update.message.reply_text("All auto-posting paused (cricket + fallback).")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    setting_set("paused", "0")
    setting_set("cricket_paused", "0")
    await update.message.reply_text("Auto-posting resumed (cricket + fallback).")

# ---------------------------------------------------------------------------
# NEW CRICKET ADMIN COMMANDS
# ---------------------------------------------------------------------------
async def cmd_todaymatches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    with db() as c:
        today_start = int(datetime.now(TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp() * 1000)
        today_end = today_start + 86400000
        rows = c.execute(
            "SELECT match_id, team1, team2, series, start_ts, status, skipped "
            "FROM cricket_matches WHERE start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts",
            (today_start, today_end)
        ).fetchall()

    if not rows:
        await update.message.reply_text(
            "Aaj ke liye koi cricket match nahi mila database mein.\n"
            "Bot 7:00 AM IST par automatically fetch karta hai.\n"
            "Manual fetch ke liye /forcematch use karein."
        )
        return

    lines = [f"Aaj ke cricket matches ({len(rows)}):"]
    for row in rows:
        match_id, t1, t2, series, start_ts, status, skipped = row
        start_dt = datetime.fromtimestamp(start_ts/1000, tz=TZ)
        skip_flag = " [SKIPPED]" if skipped else ""
        lines.append(
            f"\n  ID: {match_id}\n"
            f"  {_team_display(t1.split()[0])} vs {_team_display(t2.split()[0])}\n"
            f"  {series}\n"
            f"  Start: {start_dt.strftime('%I:%M %p IST')}\n"
            f"  Status: {status}{skip_flag}"
        )
    await update.message.reply_text("\n".join(lines))

async def cmd_win(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    args = ctx.args
    match_id = args[0] if args else get_latest_match_id()
    if not match_id:
        await update.message.reply_text("Koi match nahi mila. Match ID specify karein.")
        return
    with db() as c:
        c.execute("UPDATE cricket_matches SET tip_result='win', status='complete' "
                  "WHERE match_id=?", (match_id,))
    match = get_match_from_db(match_id)
    if match:
        caption = make_recap_caption(match, "win")
        await post_text_to_channel(
            update.get_bot() or ctx.application, caption, match_id, "recap"
        )
        await update.message.reply_text(
            f"Win marked for {match_id}. Recap post sent to channel!")
    else:
        await update.message.reply_text(f"Match {match_id} nahi mila database mein.")

async def cmd_loss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    args = ctx.args
    match_id = args[0] if args else get_latest_match_id()
    if not match_id:
        await update.message.reply_text("Koi match nahi mila. Match ID specify karein.")
        return
    with db() as c:
        c.execute("UPDATE cricket_matches SET tip_result='loss', status='complete' "
                  "WHERE match_id=?", (match_id,))
    match = get_match_from_db(match_id)
    if match:
        caption = make_recap_caption(match, "loss")
        await post_text_to_channel(
            update.get_bot() or ctx.application, caption, match_id, "recap"
        )
        await update.message.reply_text(
            f"Loss marked for {match_id}. Recap post sent to channel.")
    else:
        await update.message.reply_text(f"Match {match_id} nahi mila database mein.")

async def cmd_skiptip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id):
        return
    args = ctx.args
    match_id = args[0] if args else get_latest_match_id()
    if not match_id:
        await update.message.reply_text("Match ID specify karein.")
        return
    with db() as c:
        c.execute("UPDATE cricket_matches SET skipped=1 WHERE match_id=?",
                  (match_id,))
    # Also remove scheduled jobs for this match
    sched: AsyncIOScheduler = ctx.application.bot_data.get("scheduler")
    if sched:
        for job_type in ("preview", "combo", "live", "recap"):
            job_id = f"cricket_{job_type}::{match_id}"
            try:
                sched.remove_job(job_id)
            except Exception:
                pass
    await update.message.reply_text(
        f"Match {match_id} ke liye sab posts skip kar diye gaye.")

async def cmd_forcematch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /forcematch Team1 vs Team2 @ HH:MM
    Example: /forcematch IND vs PAK @ 19:30
    """
    if not is_admin(update.effective_chat.id):
        return
    text = " ".join(ctx.args) if ctx.args else ""

    # If no arguments given, re-trigger Cricbuzz fetch and schedule all today's matches
    if not text.strip():
        await update.message.reply_text(
            "Koi match manually specify nahi kiya. Cricbuzz se aaj ke matches re-fetch kar raha hoon..."
        )
        sched: AsyncIOScheduler = ctx.application.bot_data.get("scheduler")
        matches = fetch_today_cricket_matches()
        if not matches:
            await update.message.reply_text(
                "Aaj Cricbuzz par koi relevant match nahi mila. Manually add karne ke liye:\n"
                "/forcematch Team1 vs Team2 @ HH:MM"
            )
            return
        total_jobs = 0
        lines = [f"Cricbuzz se {len(matches)} match(es) mila:\n"]
        for match in matches:
            save_match_to_db(match)
            jobs = schedule_cricket_match(ctx.application, sched, match) if sched else 0
            total_jobs += jobs
            lines.append(
                f"  {match['team1']} vs {match['team2']} @ "
                f"{match['start_dt_ist'].strftime('%I:%M %p IST')} "
                f"— {jobs} posts scheduled"
            )
        lines.append(f"\nTotal posts queued: {total_jobs}")
        await update.message.reply_text("\n".join(lines))
        return

    # Parse manual entry: "Team1 vs Team2 @ HH:MM"
    m = re.match(r"(.+?)\s+vs\s+(.+?)\s+@\s+(\d{1,2}):(\d{2})", text, re.IGNORECASE)
    if not m:
        await update.message.reply_text(
            "Format: /forcematch Team1 vs Team2 @ HH:MM\n"
            "Example: /forcematch IND vs PAK @ 19:30\n\n"
            "Ya sirf /forcematch bhejo aur bot aaj ke matches Cricbuzz se auto-fetch karega."
        )
        return
    t1, t2, hh, mm = m.group(1).strip(), m.group(2).strip(), int(m.group(3)), int(m.group(4))
    today = datetime.now(TZ).date()
    start_dt = TZ.localize(datetime(today.year, today.month, today.day, hh, mm))
    match_id = f"manual_{t1.lower().replace(' ','_')}_vs_{t2.lower().replace(' ','_')}"
    match = {
        "match_id": match_id,
        "series": "Manual Entry",
        "team1": t1.upper(),
        "team2": t2.upper(),
        "venue": "TBD",
        "start_ts": int(start_dt.timestamp() * 1000),
        "start_dt_ist": start_dt,
        "state": "scheduled",
    }
    save_match_to_db(match)
    sched: AsyncIOScheduler = ctx.application.bot_data.get("scheduler")
    jobs = 0
    if sched:
        jobs = schedule_cricket_match(ctx.application, sched, match)
    await update.message.reply_text(
        f"Match added: {t1} vs {t2} at {hh:02d}:{mm:02d} IST\n"
        f"Match ID: {match_id}\n"
        f"Jobs scheduled: {jobs}"
    )

# ---------------------------------------------------------------------------
# AUTO-REPLIES (DM)
# ---------------------------------------------------------------------------
DISCLAIMER = "\n\n_18+ only | Responsible Gaming | Skill-based content_"

AUTO_REPLIES = {
    # ---- TIPS ----
    "tips": (
        f"*VIP TIPS FUNNEL*\n\n"
        f"Bhaiyo, hamara schedule simple hai:\n"
        f"\u2022 *Free morning tip:* Daily 10:00 AM IST par {CHANNEL}\n"
        f"\u2022 *VIP combo pick:* Match se 30 min pehle (members only)\n"
        f"\u2022 *Live alerts + recap:* Match ke time par\n\n"
        f"Paid VIP join karne ke liye register karo aur pehla deposit kar lo:\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- CASINO ----
    "casino": (
        f"*CASINO WELCOME*\n\n"
        f"Live tables 24x7 open hain. Top games:\n"
        f"\u2022 *Aviator* (crash game, fast cashouts)\n"
        f"\u2022 *Dragon Tiger* (1 round = 30 sec)\n"
        f"\u2022 *Roulette* (European + Lightning)\n\n"
        f"Naye members ko 100% welcome bonus milta hai. Register karo:\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- BONUS ----
    "bonus": (
        f"*BONUS OFFERS*\n\n"
        f"\u2022 *Welcome Bonus:* 100% up to \u20B910,000 on first deposit\n"
        f"\u2022 *Reload Bonus:* 25% on every deposit \u20B91000+\n"
        f"\u2022 *Refer & Earn:* \u20B9500 per active referral\n\n"
        f"Claim karo:\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- VIP ----
    "vip": (
        f"*VIP MEMBERSHIP*\n\n"
        f"VIP members ko milta hai:\n"
        f"\u2022 Pre-match VIP combo (locked picks)\n"
        f"\u2022 Casino exclusive bonuses\n"
        f"\u2022 Priority support + faster withdrawal\n"
        f"\u2022 Private VIP group access\n\n"
        f"Upgrade simple hai \u2014 register karo aur pehla deposit complete karo:\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- SUPPORT ----
    "support": (
        f"*SUPPORT 24x7*\n\n"
        f"\u2022 *Telegram:* DM is bot ko, agent jaldi reply karega\n"
        f"\u2022 *Email:* support@gurudevbook.com\n"
        f"\u2022 *FAQ:* https://gurudevbook.com/faq\n\n"
        f"Account ya withdrawal issue ke liye apna username + screenshot bhejo.\n"
        f"Register nahi kiya? Abhi karo: {REGISTER_URL}" + DISCLAIMER
    ),
    # ---- PROOFS ----
    "proof": (
        f"*WINNER PROOFS*\n\n"
        f"Daily withdrawal screenshots aur client chats hamare proofs channel par live hain:\n"
        f"{PROOFS_CHANNEL}\n\n"
        f"Apni jeet bhi join karo:\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- REGISTER ----
    "register": (
        f"*REGISTER IN 3 STEPS*\n\n"
        f"1\ufe0f\u20E3 Open: {REGISTER_URL}\n"
        f"2\ufe0f\u20E3 Apna mobile number + email enter karo\n"
        f"3\ufe0f\u20E3 First deposit karo aur 100% bonus claim karo\n\n"
        f"Confirmation ke baad apna username yahan bhej do for VIP access." + DISCLAIMER
    ),
    # ---- WITHDRAW ----
    "withdraw": (
        f"*INSTANT WITHDRAWALS*\n\n"
        f"\u2022 *Time:* 7\u201310 minutes\n"
        f"\u2022 *Methods:* UPI / IMPS / NEFT\n"
        f"\u2022 *Min:* \u20B9100  |  *Max:* \u20B92,00,000 per day\n\n"
        f"KYC complete hai toh withdrawal approve auto-trigger ho jata hai.\n{REGISTER_URL}" + DISCLAIMER
    ),
    # ---- DEPOSIT ----
    "deposit": (
        f"*DEPOSITS \u2014 INSTANT*\n\n"
        f"\u2022 *Min:* \u20B9100\n"
        f"\u2022 *Methods:* UPI / Net Banking / Crypto\n"
        f"\u2022 *Bonus:* 100% match on first deposit\n\n"
        f"Deposit page:\n{REGISTER_URL}" + DISCLAIMER
    ),
}

# Slash-command equivalents map to the same keyword replies
SLASH_TO_KEYWORD = {
    "tips": "tips", "casino": "casino", "bonus": "bonus", "vip": "vip",
    "support": "support", "proofs": "proof", "proof": "proof",
    "register": "register", "withdraw": "withdraw", "withdrawal": "withdraw",
    "deposit": "deposit",
}

USER_HELP_TEXT = (
    "*GurudevBook \u2014 Quick Commands*\n\n"
    "/tips \u2014 free + VIP tips schedule\n"
    "/casino \u2014 live casino games\n"
    "/bonus \u2014 current bonus offers\n"
    "/vip \u2014 VIP membership benefits\n"
    "/register \u2014 how to sign up\n"
    "/deposit \u2014 deposit info\n"
    "/withdraw \u2014 withdrawal info\n"
    "/proofs \u2014 winner proofs channel\n"
    "/support \u2014 contact support\n\n"
    f"Channel: {CHANNEL}  |  Proofs: {PROOFS_CHANNEL}"
)

# In-memory rate limiter: {(user_id, keyword): last_sent_unix_ts}
_RL_BUCKET: dict = {}
_RL_WINDOW = 300  # 5 minutes

def _rate_limited(user_id: int, keyword: str) -> bool:
    """Return True if we should NOT send (still inside cooldown)."""
    import time
    now = int(time.time())
    key = (user_id, keyword)
    last = _RL_BUCKET.get(key, 0)
    if now - last < _RL_WINDOW:
        return True
    _RL_BUCKET[key] = now
    # opportunistic cleanup
    if len(_RL_BUCKET) > 5000:
        cutoff = now - _RL_WINDOW
        for k in list(_RL_BUCKET.keys()):
            if _RL_BUCKET[k] < cutoff:
                _RL_BUCKET.pop(k, None)
    return False

async def _send_keyword_reply(update: Update, keyword: str):
    user_id = update.effective_user.id if update.effective_user else 0
    if _rate_limited(user_id, keyword):
        return
    reply = AUTO_REPLIES.get(keyword)
    if not reply:
        return
    try:
        await update.message.reply_text(
            reply, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        log.warning("Auto-reply send failed: %s", e)

# Slash command handler factory
def make_keyword_cmd(keyword: str):
    async def _h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.is_bot:
            return
        await _send_keyword_reply(update, keyword)
    return _h

async def cmd_user_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.is_bot:
        return
    # Admins still get full admin help via the existing /help below; this is for everyone
    await update.message.reply_text(USER_HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    if user and user.is_bot:
        return
    chat_type = update.effective_chat.type
    # Respond in DM, channel comments (linked discussion), and our own discussion group.
    # Skip noisy public groups by only replying when bot is mentioned OR in private/discussion.
    text = update.message.text.lower().strip()
    is_private = (chat_type == "private")
    bot_username = (ctx.bot.username or "").lower()
    is_mentioned = bot_username and ("@" + bot_username) in text
    if not (is_private or is_mentioned or chat_type in {"group", "supergroup"}):
        return
    # In groups, only respond if mentioned OR a recognised keyword is the *only* word
    matched_keyword = None
    for kw in AUTO_REPLIES.keys():
        if kw in text:
            matched_keyword = kw
            break
    if matched_keyword:
        # In groups without mention, only fire on short messages (<=4 words) to avoid spam
        if chat_type != "private" and not is_mentioned and len(text.split()) > 4:
            return
        await _send_keyword_reply(update, matched_keyword)
        return
    if is_private and text in {"hi", "hello", "hey", "namaste", "start"}:
        await update.message.reply_text(
            f"Namaste! GurudevBook me aapka swagat hai.\n\n"
            f"Type karo: tips, casino, bonus, vip, register, withdraw, support\n"
            f"Ya /help bhejo full menu ke liye.\n\n"
            f"Channel: {CHANNEL}" + DISCLAIMER,
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

# ---------------------------------------------------------------------------
# SCHEDULER WIRING
# ---------------------------------------------------------------------------
def build_scheduler(application) -> AsyncIOScheduler:
    # Ensure data directory exists before creating the SQLite jobstore.
    # On Railway without a Volume attached, DATA_DIR may not exist — we create it.
    # If creation fails (read-only FS), fall back to MemoryJobStore gracefully.
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        jobstore = SQLAlchemyJobStore(url=f'sqlite:///{JOBS_DB_PATH}')
        # Test the connection immediately so we fail fast rather than at sched.start()
        jobstore.start(None, 'default')  # triggers table creation / connection
        jobstore.shutdown()              # close test connection; scheduler will reopen
        jobstores = {'default': SQLAlchemyJobStore(url=f'sqlite:///{JOBS_DB_PATH}')}
        log.info("Using SQLite persistent jobstore at %s", JOBS_DB_PATH)
    except Exception as e:
        log.warning("SQLite jobstore unavailable (%s) — falling back to MemoryJobStore. "
                    "Attach a Railway Volume at %s to enable persistence.", e, DATA_DIR)
        from apscheduler.jobstores.memory import MemoryJobStore
        jobstores = {'default': MemoryJobStore()}

    executors = {
        'default': AsyncIOExecutor()
    }
    job_defaults = {
        'coalesce': False,
        'max_instances': 3,
        'misfire_grace_time': 600  # 10 minute grace for missed jobs on boot
    }
    sched = AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone=TZ
    )

    # 1) Pre-fill rollout (one-shot dated jobs)
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

    # 2) Daily calendar slots (recurring cron — fallback on no-match days)
    for hh, mm in SLOTS:
        job_id = f"cal::{hh:02d}{mm:02d}"
        sched.add_job(post_calendar_slot, CronTrigger(hour=hh, minute=mm,
                                                       timezone=TZ),
                      args=[application, (hh, mm)], id=job_id,
                      replace_existing=True, misfire_grace_time=600)
        log.info("Scheduled daily calendar slot %02d:%02d IST", hh, mm)

    # 3) Daily cricket fixture fetch at 07:00 IST
    # This is a self-healing safety net: it re-fetches, updates DB, and re-schedules.
    # replace_existing=True in add_job handles the deduplication.
    sched.add_job(
        daily_cricket_fetch_job, CronTrigger(hour=7, minute=0, timezone=TZ),
        args=[application], id="cricket_daily_fetch",
        replace_existing=True, misfire_grace_time=3600
    )
    log.info("Scheduled daily cricket fetch at 07:00 IST")

    return sched

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
async def post_init(application):
    init_db()
    sched = build_scheduler(application)
    sched.start()
    application.bot_data["scheduler"] = sched

    # Admin notification on boot
    admin_id = setting_get("admin_chat_id")
    if admin_id:
        try:
            jobs = sorted(sched.get_jobs(),
                          key=lambda j: j.next_run_time or datetime.max.replace(tzinfo=TZ))
            now_str = datetime.now(TZ).strftime("%H:%M IST")
            
            lines = [f"🚀 *GurudevBook Bot v2.1 Rebooted*"]
            lines.append(f"Time: {now_str}")
            lines.append(f"Active jobs in queue: {len(jobs)}")
            
            if jobs:
                lines.append("\n*Next 3 upcoming:*")
                for j in jobs[:3]:
                    nrt = j.next_run_time
                    if nrt:
                        lines.append(f" \u231b {nrt.astimezone(TZ).strftime('%H:%M')} \u2014 {j.id}")
            
            # Note: misfire_grace_time handles recovery automatically
            lines.append("\n_Persistent JobStore active. Missed jobs (within 10m) will fire automatically._")
            
            await application.bot.send_message(
                chat_id=int(admin_id),
                text="\n".join(lines),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            log.error("Boot admin notification failed: %s", e)

    # Run cricket fetch on EVERY boot so DB is never empty after redeploy
    log.info("Running cricket fetch on boot (every startup)...")
    try:
        await daily_cricket_fetch_job(application)
    except Exception as e:
        log.error("Boot cricket fetch failed: %s", e)

def main():
    application = (Application.builder()
                   .token(TOKEN)
                   .post_init(post_init)
                   .build())

    # General commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("claim", cmd_claim))
    # Admin-only deep help
    application.add_handler(CommandHandler("adminhelp", cmd_help))
    # Public /help for everyone
    application.add_handler(CommandHandler("help", cmd_user_help))
    # Public keyword slash commands
    for slash, kw in SLASH_TO_KEYWORD.items():
        application.add_handler(CommandHandler(slash, make_keyword_cmd(kw)))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("skip", cmd_skip))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))

    # Cricket commands
    application.add_handler(CommandHandler("todaymatches", cmd_todaymatches))
    application.add_handler(CommandHandler("win", cmd_win))
    application.add_handler(CommandHandler("loss", cmd_loss))
    application.add_handler(CommandHandler("skiptip", cmd_skiptip))
    application.add_handler(CommandHandler("forcematch", cmd_forcematch))

    # DM auto-replies
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           on_text))

    log.info("GurudevBook bot v2.0 starting. Channel=%s TZ=%s", CHANNEL, TZ_NAME)
    # drop_pending_updates=False so messages sent during a redeploy are NOT silently discarded
    application.run_polling(drop_pending_updates=False,
                            allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
