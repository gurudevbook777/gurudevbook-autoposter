# GurudevBook Official — Telegram Auto-Poster Bot

Automated 24/7 Telegram posting bot for **[GurudevBook Official](https://gurudevbook.com)** — India's premium sports tips and casino gaming channel.

## What It Does

- Posts **30 trust-building pre-fill posts** over the first 3 days (winner proofs, match results, casino wins, FAQs) at 10:00, 13:00, 18:00, and 21:30 IST.
- After pre-fill completes, loops a **28-post weekly calendar** (4 posts/day × 7 days) indefinitely at the same IST slots.
- Auto-pins the "Beware of fake channels" warning and the founder intro post.
- Responds to DM keywords: `tips`, `casino`, `bonus`, `register`, `vip`, `support` with Hinglish replies and the registration CTA.
- Admin commands via DM: `/claim`, `/status`, `/list`, `/skip`, `/pause`, `/resume`, `/help`.

## Deploy to Railway

### Step 1 — Connect this repo to Railway

1. Go to **https://railway.com/new**
2. Click **"GitHub Repository"**
3. If first time: click **"Configure GitHub App"** → install Railway on your GitHub account → grant access to **gurudevbook-autoposter** repo
4. Select **gurudevbook-autoposter** from the list
5. Click **Deploy**

### Step 2 — Set Environment Variables

Once the project opens, click the service → **Variables** tab → **Raw Editor**, then paste:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
SMOKE_TEST_ON_BOOT=1
TZ=Asia/Kolkata
CHANNEL_USERNAME=@gurudevbook_official
```

Click **Update** → **Redeploy**.

### Step 3 — Claim Admin Access

Open Telegram, find your bot, and send `/claim` in a private DM. The bot will confirm you as admin.

## Required Environment Variables

| Variable | Required | Description |
|:--|:--|:--|
| `TELEGRAM_BOT_TOKEN` | Yes | Your bot token from BotFather |
| `SMOKE_TEST_ON_BOOT` | Yes (first deploy) | Set to `1` to send a live confirmation message on first boot |
| `TZ` | Yes | Timezone — use `Asia/Kolkata` |
| `CHANNEL_USERNAME` | Yes | Your channel handle, e.g. `@gurudevbook_official` |
| `REGISTER_URL` | Optional | CTA link (default: `https://gurudevbook.com/register`) |

## Content Structure

```
content/
  prefill/     ← 30 trust pre-fill PNGs (posted over first 3 days)
  calendar/    ← 28 daily calendar PNGs (Day 1–7, 4 slots/day)
  captions.json ← All captions paired to filenames
```

## Admin Commands

Send these as DMs to the bot after `/claim`:

| Command | Action |
|:--|:--|
| `/status` | Show next 5 scheduled posts |
| `/list` | Show full upcoming queue |
| `/skip` | Skip the next post |
| `/pause` | Pause all auto-posting |
| `/resume` | Resume auto-posting |
| `/help` | Show this command list |

## Tech Stack

- Python 3.11
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21
- APScheduler 3.10 (cron + one-shot jobs, Asia/Kolkata timezone)
- SQLite (persistent state: sent posts, admin ID, scheduler state)

---

*For support, visit [gurudevbook.com](https://gurudevbook.com)*
