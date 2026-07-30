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
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY")

# How far back to accept postings (a little over 24h as a safety buffer,
# since we run hourly and sources report "updated" times inconsistently)
MAX_AGE_HOURS = 26

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
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
# Filtering
# ---------------------------------------------------------------------------

def parse_updated(value):
    """Best-effort parse of a job's 'updated' timestamp into an aware datetime."""
    if not value:
        return None
    formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    v = value.replace("Z", "+00:00") if isinstance(value, str) else value
    for fmt in formats:
        try:
            dt = datetime.strptime(v, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_recent(job):
    dt = parse_updated(job.get("updated"))
    if dt is None:
        # Unknown date: don't discard outright, dedup will still protect us.
        return True
    age = datetime.now(timezone.utc) - dt
    return age <= timedelta(hours=MAX_AGE_HOURS)


def matches_medical_device(job):
    haystack = f"{job.get('title', '')} {job.get('snippet', '')}".lower()
    return any(kw in haystack for kw in MEDICAL_KEYWORDS)


# ---------------------------------------------------------------------------
# Telegram posting
# ---------------------------------------------------------------------------

def send_to_telegram(job):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.", file=sys.stderr)
        return False

    title = escape_html(job["title"] or "New position")
    company = escape_html(job["company"] or "Unknown company")
    location = escape_html(job["location"] or "Location not specified")
    snippet = escape_html(job["snippet"])
    if len(snippet) > 220:
        snippet = snippet[:220].rsplit(" ", 1)[0] + "..."
    link = job["link"]

    text = f"<b>{title}</b>\n{company} | {location}\n"
    if snippet:
        text += f"{snippet}\n"
    text += f"\n🔗 <a href=\"{link}\">Apply here</a>"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"ERROR sending to Telegram: {resp.status_code} {resp.text}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as e:
        print(f"ERROR sending to Telegram: {e}", file=sys.stderr)
        return False


def escape_html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = prune_state(load_state())

    all_jobs = []
    all_jobs.extend(fetch_jooble_jobs())
    all_jobs.extend(fetch_greenhouse_jobs())
    all_jobs.extend(fetch_lever_jobs())

    print(f"Fetched {len(all_jobs)} total jobs across all sources.")

    new_count = 0
    sent_count = 0

    for job in all_jobs:
        if not job.get("link") or not job.get("title"):
            continue
        if not matches_medical_device(job):
            continue
        if not is_recent(job):
            continue

        h = job_hash(job["link"], job["title"], job["company"])
        if h in state:
            continue

        new_count += 1
        if send_to_telegram(job):
            sent_count += 1
            state[h] = datetime.now(timezone.utc).isoformat()
            time.sleep(1.5)  # gentle rate limiting on Telegram sends

    save_state(state)
    print(f"New matching jobs found: {new_count}. Successfully posted: {sent_count}.")


if __name__ == "__main__":
    main()
