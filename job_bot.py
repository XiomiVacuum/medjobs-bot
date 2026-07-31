#!/usr/bin/env python3
"""
Medical Device Job Bot
-----------------------
Searches for medical device job postings (Israel + remote/global) from the
last ~24 hours and posts new ones to a Telegram group.

Sources:
  1. Jooble API (free, aggregates thousands of job boards/company sites)
  2. Optional: Greenhouse / Lever company career-page APIs (add your own
     board tokens in COMPANY_BOARDS below for tighter, company-specific
     coverage of the medical device space)

Runs statelessly except for posted_jobs.json, which tracks what's already
been sent so nothing is posted twice. Designed to run on a schedule
(e.g. GitHub Actions, hourly) with no manual intervention.
"""

import os
import sys
import json
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY")

# How far back to accept postings. Widened to cover the weekend blackout
# window (~32h) with a buffer, so nothing posted just before or during the
# pause gets silently skipped once posting resumes.
MAX_AGE_HOURS = 40

# Jooble searches to run. Add/adjust as needed.
JOOBLE_SEARCHES = [
    {"keywords": "medical device", "location": "Israel"},
    {"keywords": "medical device", "location": "Remote"},
    {"keywords": "medical devices", "location": "Israel"},
]

# Optional: Greenhouse/Lever board tokens for specific medical device
# companies you want guaranteed coverage of. Find a company's token from
# their careers URL, e.g. https://boards.greenhouse.io/<TOKEN> or
# https://jobs.lever.co/<TOKEN>. Leave empty to skip this source.
GREENHOUSE_BOARDS = [
    # "example-medtech-co",
]
LEVER_BOARDS = [
    # "example-medtech-co",
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "posted_jobs.json")
STATE_RETENTION_DAYS = 14  # prune dedup records older than this

MDI_JOBS_URL = "https://medical-device.co.il/jobs/"

# No new postings during the weekend: Friday 13:00 through Saturday 21:00,
# Israel local time (handles daylight saving automatically).
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
BLACKOUT_START_WEEKDAY = 4   # Friday (Monday=0)
BLACKOUT_START_HOUR = 13
BLACKOUT_END_WEEKDAY = 5     # Saturday
BLACKOUT_END_HOUR = 21


def in_weekend_blackout():
    now = datetime.now(ISRAEL_TZ)
    if now.weekday() == BLACKOUT_START_WEEKDAY and now.hour >= BLACKOUT_START_HOUR:
        return True
    if now.weekday() == BLACKOUT_END_WEEKDAY and now.hour < BLACKOUT_END_HOUR:
        return True
    return False

MEDICAL_KEYWORDS = [
    "medical device", "medical devices", "medtech", "med-tech",
    "biomedical", "regulatory affairs", "clinical affairs",
    "quality assurance", "ra/qa", "qa/ra", "diagnostics",
    "implant", "surgical", "in vitro diagnostic", "ivd",
]

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# State (dedup) handling
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}, ""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
            return json.loads(raw), raw
    except (json.JSONDecodeError, OSError):
        return {}, ""


def save_state(state, original_raw):
    new_raw = json.dumps(state, ensure_ascii=False, indent=2)
    if new_raw == (original_raw or "").strip():
        return  # no real change, avoid a pointless commit/push
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(new_raw + "\n")


def prune_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    pruned = {}
    for job_hash, added_at in state.items():
        try:
            if datetime.fromisoformat(added_at) > cutoff:
                pruned[job_hash] = added_at
        except ValueError:
            continue
    return pruned


def job_hash(link, title, company):
    raw = f"{link}|{title}|{company}".lower().strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
