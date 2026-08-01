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

        for h in headings:
            a = h.find("a", href=lambda x: x and "/job/" in x)
            title = a.get_text(strip=True)
            link = a["href"].strip()
            if not title or not link:
                continue

            company = category = location = date_text = ""
            is_new = False

            node = h.find_next_sibling()
            steps = 0
            while node and node.name not in ("h2", "h3") and steps < 12:
                text = node.get_text(" ", strip=True)
                if "new job" in text.lower():
                    is_new = True
                if node.name == "ul":
                    lis = [li.get_text(strip=True) for li in node.find_all("li")]
                    if len(lis) >= 1:
                        company = lis[0] if len(lis) > 0 else ""
                        category = lis[1] if len(lis) > 1 else ""
                        location = lis[2] if len(lis) > 2 else ""
                        date_text = lis[3] if len(lis) > 3 else ""
                node = node.find_next_sibling()
                steps += 1

            if not is_new:
                continue  # only take listings MDI itself flags as recent

            snippet_parts = [p for p in [category, location, date_text] if p]
            results.append({
                "title": title,
                "company": company or "MDI listing",
                "location": location or "Israel",
                "snippet": " | ".join(snippet_parts),
                "link": link,
                "updated": datetime.now(timezone.utc).isoformat(),
                "source": "MDI",
            })

        print(f"MDI: {len(results)} new-flagged listings found on page 1")
    except requests.RequestException as e:
        print(f"ERROR fetching MDI jobs page: {e}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR parsing MDI jobs page: {e}", file=sys.stderr)

    return results


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
    if job.get("source") == "MDI":
        return True  # every MDI listing is already medical device / Israel by definition
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
    now_il = datetime.now(ISRAEL_TZ)
    print(f"Job bot started. Current Israel time: {now_il.strftime('%A %Y-%m-%d %H:%M')}")
    sys.stdout.flush()

    if in_weekend_blackout():
        print("Weekend blackout window (Fri 13:00 - Sat 21:00 Israel time) - skipping this run.")
        sys.stdout.flush()
        return

    raw_state, original_raw = load_state()
    state = prune_state(raw_state)

    all_jobs = []
    all_jobs.extend(fetch_jooble_jobs())
    all_jobs.extend(fetch_greenhouse_jobs())
    all_jobs.extend(fetch_lever_jobs())
    all_jobs.extend(fetch_mdi_jobs())

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

    save_state(state, original_raw)
    print(f"New matching jobs found: {new_count}. Successfully posted: {sent_count}.")


if __name__ == "__main__":
    main()
