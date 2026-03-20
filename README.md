# Twitter-to-Bluesky Mirror Bot

Automatically mirrors tweets from [@sportz_nutt51](https://x.com/sportz_nutt51) to [sportz-nutt51-bot.bsky.social](https://bsky.app/profile/sportz-nutt51-bot.bsky.social) via Nitter RSS feeds.

## How It Works

- A GitHub Actions workflow runs every 15 minutes
- It fetches the latest tweets from a Nitter RSS feed
- New tweets (not already in `posted.txt`) are posted to Bluesky
- `posted.txt` is committed back to the repo to track what's been posted

## Setup

### 1. Fork this repo

Click the **Fork** button at the top right of the GitHub page.

### 2. Create a Bluesky App Password

1. Log in to [bsky.app](https://bsky.app) with the bot account (`sportz-nutt51-bot.bsky.social`)
2. Go to **Settings → App Passwords**
3. Click **Add App Password**, give it a name (e.g., `mirror-bot`), and copy the generated password

### 3. Add the secret to GitHub

1. In your forked repo, go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `BSKY_APP_PASSWORD`
4. Value: paste the app password from step 2
5. Click **Add secret**

### 4. Enable the workflow

1. Go to the **Actions** tab in your forked repo
2. Click **I understand my workflows, go ahead and enable them**
3. The workflow will now run automatically every 15 minutes

### 5. Verify it's working

- Go to the **Actions** tab and check for successful workflow runs
- You can also click **Run workflow** to trigger it manually
- Check the bot's Bluesky profile to see if new posts appear
- Review the workflow logs for any error messages

## Running Locally

```bash
pip install -r requirements.txt
export BSKY_APP_PASSWORD="your-app-password-here"
python mirror.py
```

## Customization

To mirror a different account, edit these values in `mirror.py`:

- `FEED_URLS` — the Nitter RSS feed URLs
- `BSKY_HANDLE` — the destination Bluesky account
- `TWITTER_USERNAME` — used for normalizing tweet URLs
