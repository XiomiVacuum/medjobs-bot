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


# ---------------------------------------------------------------------------
# Source: Jooble
# ---------------------------------------------------------------------------

def fetch_jooble_jobs():
    if not JOOBLE_API_KEY:
        print("WARNING: JOOBLE_API_KEY not set, skipping Jooble source.")
        return []

    results = []
    url = f"https://jooble.org/api/{JOOBLE_API_KEY}"

    for search in JOOBLE_SEARCHES:
        try:
            resp = requests.post(url, json=search, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", [])
            print(f"Jooble: {len(jobs)} results for {search}")
            for j in jobs:
                results.append({
                    "title": j.get("title", "").strip(),
                    "company": j.get("company", "").strip(),
                    "location": j.get("location", "").strip(),
                    "snippet": j.get("snippet", "").strip(),
                    "link": j.get("link", "").strip(),
                    "updated": j.get("updated", ""),
                    "source": "Jooble",
                })
        except requests.RequestException as e:
            print(f"ERROR fetching Jooble for {search}: {e}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Source: Greenhouse
# ---------------------------------------------------------------------------

def fetch_greenhouse_jobs():
    results = []
    for token in GREENHOUSE_BOARDS:
        try:
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            for j in data.get("jobs", []):
                results.append({
                    "title": j.get("title", "").strip(),
                    "company": token,
                    "location": (j.get("location") or {}).get("name", "").strip(),
                    "snippet": strip_html(j.get("content", ""))[:280],
                    "link": j.get("absolute_url", "").strip(),
                    "updated": j.get("updated_at", ""),
                    "source": "Greenhouse",
                })
        except requests.RequestException as e:
            print(f"ERROR fetching Greenhouse board '{token}': {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Source: Lever
# ---------------------------------------------------------------------------

def fetch_lever_jobs():
    results = []
    for token in LEVER_BOARDS:
        try:
            url = f"https://api.lever.co/v0/postings/{token}?mode=json"
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            for j in data:
                created_ms = j.get("createdAt")
                updated_iso = ""
                if created_ms:
                    updated_iso = datetime.fromtimestamp(
                        created_ms / 1000, tz=timezone.utc
                    ).isoformat()
                categories = j.get("categories", {}) or {}
                results.append({
                    "title": j.get("text", "").strip(),
                    "company": token,
                    "location": categories.get("location", "").strip(),
                    "snippet": strip_html(j.get("descriptionPlain", j.get("description", "")))[:280],
                    "link": j.get("hostedUrl", "").strip(),
                    "updated": updated_iso,
                    "source": "Lever",
                })
        except requests.RequestException as e:
            print(f"ERROR fetching Lever board '{token}': {e}", file=sys.stderr)
    return results


def strip_html(text):
    import re
    return re.sub(r"<[^>]+>", " ", text or "").replace("&amp;", "&").strip()


# ---------------------------------------------------------------------------
# Source: MDI (Israeli medical device community job board)
# ---------------------------------------------------------------------------

def fetch_mdi_jobs():
    """
    Parses the public MDI jobs listing page. Every listing on this site is
    by definition a medical device role in Israel, so these bypass the
    keyword filter. The site doesn't expose exact posting timestamps, so we
    use its own "New Job" badge as a recency signal, and rely on dedup to
    prevent repeats. If MDI ever redesigns their page, this function may
    need updating (unlike the API-based sources above).
    """
    results = []
    try:
        resp = requests.get(MDI_JOBS_URL, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        headings = [h for h in soup.find_all(["h2", "h3"])
                    if h.find("a", href=lambda x: x and "/job/" in x)]
        print(f"MDI: HTTP {resp.status_code}, page length {len(resp.text)} chars, "
              f"{len(headings)} job headings found")

        for h in headings:
            a = h.find("a", href=lambda x: x and "/job/" in x)
            title = a.get_text(strip=True)
            link = a["href"].strip()
            if not
