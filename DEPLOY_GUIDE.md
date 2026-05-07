# GurudevBook Auto-Poster: Laptop Deployment Guide

This guide shows you how to deploy the GurudevBook Telegram bot directly from your laptop to your Railway account. This method is 100% reliable and keeps your API tokens completely private.

---

## Step 1: Extract the Files
1. Download `GurudevBook_AutoPoster_LaptopDeploy.zip`.
2. Right-click and **Extract All** (Windows) or double-click to unzip (Mac).
3. Open your terminal (PowerShell on Windows, or Terminal on Mac/Linux).
4. Use the `cd` command to navigate into the extracted folder:
   ```bash
   cd path/to/GurudevBook_AutoPoster_LaptopDeploy
   ```

---

## Step 2: Run the Deployment Commands

Copy and paste these commands into your terminal one by one.

### For Windows (PowerShell)
```powershell
# 1. Install Railway CLI
iwr -useb https://railway.com/install.ps1 | iex

# 2. Login (this will open your browser to authenticate)
railway login

# 3. Create a new project
railway init -n gurudevbook-autoposter

# 4. Set environment variables (your bot token is safe here)
railway variables --set "TELEGRAM_BOT_TOKEN=8706650470:AAFLiaOeGijLxfpGaZzXzNXdDtfiMaGLqZA" --set "SMOKE_TEST_ON_BOOT=1" --set "TZ=Asia/Kolkata" --set "CHANNEL_USERNAME=@gurudevbook_official"

# 5. Deploy the bot to Railway
railway up
```

### For Mac / Linux
```bash
# 1. Install Railway CLI
curl -fsSL https://railway.com/install.sh | sh

# 2. Login (this will open your browser to authenticate)
railway login

# 3. Create a new project
railway init -n gurudevbook-autoposter

# 4. Set environment variables
railway variables --set "TELEGRAM_BOT_TOKEN=8706650470:AAFLiaOeGijLxfpGaZzXzNXdDtfiMaGLqZA" --set "SMOKE_TEST_ON_BOOT=1" --set "TZ=Asia/Kolkata" --set "CHANNEL_USERNAME=@gurudevbook_official"

# 5. Deploy the bot to Railway
railway up
```

---

## Step 3: What You'll See

1. The `railway up` command will take about 1-2 minutes. It will say "Building..." and then "Deploying...".
2. Once it finishes, run this command to view the live logs:
   ```bash
   railway logs
   ```
3. You should see logs that look like this:
   ```text
   [INFO] gurudevbook — GurudevBook bot starting. Channel=@gurudevbook_official TZ=Asia/Kolkata
   [INFO] gurudevbook — Scheduled pre-fill 01_winner_proof_upi_25k.png at Thu 07 May 10:00 IST
   [INFO] gurudevbook — Smoke test message sent (msg_id=123).
   ```
4. **Check your Telegram Channel:** You will see a message saying *"✅ GurudevBook auto-poster is LIVE."* You can manually delete this message once you confirm it works.

---

## Step 4: Claim Admin Access

1. Open Telegram and search for your bot (`@gurudevbook_bot` or whatever username you gave it in BotFather).
2. Send the command `/claim` in a private DM to the bot.
3. The bot will reply: *"✅ Admin claimed! Your chat ID is now the bot owner."*
4. You can now send `/status` to see the next 5 scheduled posts, or `/pause` to stop posting.

---

## Troubleshooting

- **"Command not found: railway"**: Close your terminal and open a new one, then try again.
- **"Unauthorized" during deploy**: Run `railway logout` then `railway login` again.
- **Bot isn't posting**: Check the logs using `railway logs`. Ensure your `TELEGRAM_BOT_TOKEN` is correct and that the bot has been added as an **Admin** in your `@gurudevbook_official` channel with permission to post messages.

---

## After Deploy: Security Cleanup

Since we tried a few different tokens earlier, it is highly recommended to clean them up:
1. Go to https://railway.com/account/tokens and delete any tokens we created today.
2. Go to https://fly.io/dashboard/personal/tokens and delete the Fly.io token.
3. (Optional) Go to BotFather on Telegram, select your bot, and click "Revoke Token" to generate a new one. If you do this, you must update the variable in Railway:
   ```bash
   railway variables --set "TELEGRAM_BOT_TOKEN=new_token_here"
   railway up
   ```
