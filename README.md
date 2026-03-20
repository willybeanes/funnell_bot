# Twitter-to-Bluesky Mirror Bot

Automatically mirrors tweets from [@sportz_nutt51](https://x.com/sportz_nutt51) to [sportz-nutt51-bot.bsky.social](https://bsky.app/profile/sportz-nutt51-bot.bsky.social) using [Twikit](https://github.com/d60/twikit) to fetch tweets.

## How It Works

- A GitHub Actions workflow runs every 15 minutes
- It fetches the latest tweets using Twikit (Twitter's internal API)
- New tweets (not already in `posted.txt`) are posted to Bluesky
- `posted.txt` is committed back to the repo to track what's been posted
- Twitter session cookies are cached between runs to avoid repeated logins

## Setup

### 1. Fork this repo

Click the **Fork** button at the top right of the GitHub page.

### 2. Create a Bluesky App Password

1. Log in to [bsky.app](https://bsky.app) with the bot account (`sportz-nutt51-bot.bsky.social`)
2. Go to **Settings → App Passwords**
3. Click **Add App Password**, give it a name (e.g., `mirror-bot`), and copy the generated password

### 3. Add secrets to GitHub

In your forked repo, go to **Settings → Secrets and variables → Actions** and add these secrets:

| Secret | Description |
|--------|-------------|
| `BSKY_APP_PASSWORD` | Bluesky app password from step 2 |
| `TWITTER_USERNAME` | Twitter/X username for login (the account used to scrape) |
| `TWITTER_EMAIL` | Email associated with the Twitter/X login account |
| `TWITTER_PASSWORD` | Password for the Twitter/X login account |

> **Note:** The Twitter credentials are for the account *reading* tweets (can be any account, including a burner). This is NOT the account being mirrored — that's configured in `mirror.py` as `TWITTER_USERNAME`.

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
export BSKY_APP_PASSWORD="your-app-password"
export TWITTER_USERNAME="your-twitter-login"
export TWITTER_EMAIL="your-twitter-email"
export TWITTER_PASSWORD="your-twitter-password"
python mirror.py
```

## Customization

To mirror a different account, edit `TWITTER_USERNAME` (the target account) and `BSKY_HANDLE` in `mirror.py`.
