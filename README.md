# Medical Device Job Bot → Telegram

Automatically finds medical device job postings (Israel + remote/global) from
the last ~24 hours and posts them to your Telegram group, fully unattended,
running on GitHub's free tier (no server, no cost).

## What it does
- Runs **every hour** via GitHub Actions
- Searches the free **Jooble API** (aggregates thousands of job boards and
  company career sites) for "medical device" roles
- Filters out anything older than ~26 hours and anything already posted
  (tracked in `posted_jobs.json`)
- Posts new matches to your Telegram group as: **Title**, company/location,
  short description, and an "Apply here" link

## One-time setup (about 10 minutes)

### 1. Get a free Jooble API key
1. Go to https://jooble.org/api/about and fill in the short request form.
2. Jooble emails you a personal API key. Save it.

### 2. Get your Telegram group's Chat ID
Since your bot is already an admin in the group:
1. Send any message in the group (e.g. "test").
2. In a browser, visit:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   (replace `<YOUR_BOT_TOKEN>` with your bot's token)
3. Look for `"chat":{"id":-100XXXXXXXXXX, ...}` in the response — that
   negative number is your `TELEGRAM_CHAT_ID`.

### 3. Create a GitHub repository
1. Create a new **private** repository on GitHub (e.g. `medjobs-bot`).
2. Upload all the files in this folder to it (including the
   `.github/workflows/job_search.yml` file — keep that exact folder path).

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add three secrets:
| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot's token (from BotFather) |
| `TELEGRAM_CHAT_ID` | the chat ID you found in step 2 |
| `JOOBLE_API_KEY` | the key you got in step 1 |

### 5. Turn it on
1. Go to the **Actions** tab of your repo → you should see "Medical Device
   Job Search". Click **Enable workflow** if prompted.
2. Click **Run workflow** to test it manually once.
3. Check your Telegram group — you should see matching postings appear
   within a minute or two (or nothing, if there's nothing new right now —
   that's normal).

From then on, it runs automatically every hour, forever, with no further
action from you.

## Tuning it later
- **Change frequency:** edit the `cron` line in
  `.github/workflows/job_search.yml` (currently every hour).
- **Add specific companies:** if there are specific medical device companies
  you want guaranteed coverage of, and they use Greenhouse or Lever as their
  ATS, add their board token to `GREENHOUSE_BOARDS` or `LEVER_BOARDS` in
  `job_bot.py`. (Find the token in their careers page URL, e.g.
  `boards.greenhouse.io/<token>`.)
- **Adjust keywords:** edit `MEDICAL_KEYWORDS` and `JOOBLE_SEARCHES` in
  `job_bot.py` to broaden or narrow what counts as a match.
- **Change the "how far back" window:** edit `MAX_AGE_HOURS` in `job_bot.py`.

## Notes & limitations
- LinkedIn and Facebook are intentionally not included as sources — they
  don't offer a public API for this, and scraping them directly breaks their
  Terms of Service and tends to get blocked, which would defeat the "fully
  automated, no maintenance" goal. Jooble's aggregation picks up many
  listings that are cross-posted from these platforms onto public job
  boards and company sites, but not everything on LinkedIn/Facebook
  specifically.
- If you ever want tighter LinkedIn coverage, that requires either LinkedIn's
  paid Talent/Recruiter API (business partnership) or a paid scraping
  service — happy to help you add that as a second phase if it becomes
  important.
