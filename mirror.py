#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twikit GuestClient for tweet fetching."""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

from atproto import Client, client_utils
from twikit.guest import GuestClient

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
TWITTER_USERNAME = "sportz_nutt51"


def load_posted() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    return set(POSTED_FILE.read_text().strip().splitlines())


def save_posted(url: str) -> None:
    with open(POSTED_FILE, "a") as f:
        f.write(url + "\n")


async def fetch_tweets() -> list[dict] | None:
    """Fetch recent tweets using Twikit GuestClient (no login required)."""
    client = GuestClient()

    try:
        await client.activate()
        print("Activated guest token")
    except Exception as e:
        print(f"Error activating guest token: {e}")
        return None

    try:
        user = await client.get_user_by_screen_name(TWITTER_USERNAME)
        tweets = await client.get_user_tweets(user.id, "Tweets", count=20)
        print(f"Fetched {len(tweets)} tweets from @{TWITTER_USERNAME}")

        results = []
        for tweet in tweets:
            tweet_url = f"https://x.com/{TWITTER_USERNAME}/status/{tweet.id}"
            results.append({
                "text": tweet.text or "",
                "url": tweet_url,
                "id": tweet.id,
            })
        return results
    except Exception as e:
        print(f"Error fetching tweets: {e}")
        return None


def clean_tweet_text(text: str) -> str:
    # Strip leading "RT @username: " prefix
    text = re.sub(r"^RT @\w+:\s*", "", text)
    return text.strip()


def format_post(text: str, tweet_url: str) -> str:
    suffix = f"\n\n🐦 {tweet_url}"

    if len(text) + len(suffix) > BSKY_CHAR_LIMIT:
        truncation_marker = f"… [full tweet: {tweet_url}]"
        available = BSKY_CHAR_LIMIT - len(truncation_marker) - len(suffix)
        text = text[:available] + truncation_marker

    return text + suffix


def create_rich_post(client: Client, post_text: str, tweet_url: str):
    """Create a post with a clickable link facet for the tweet URL."""
    tb = client_utils.TextBuilder()

    url_start = post_text.find(tweet_url)
    if url_start == -1:
        tb.text(post_text)
    else:
        tb.text(post_text[:url_start])
        tb.link(tweet_url, tweet_url)
        remaining = post_text[url_start + len(tweet_url):]
        if remaining:
            tb.text(remaining)

    return tb


def main():
    bsky_password = os.environ.get("BSKY_APP_PASSWORD")
    if not bsky_password:
        print("Error: BSKY_APP_PASSWORD environment variable not set")
        sys.exit(1)

    # Fetch tweets via Twikit
    tweet_items = asyncio.run(fetch_tweets())
    if tweet_items is None:
        print("Error: Could not fetch tweets")
        sys.exit(1)

    posted = load_posted()

    # Collect new items, oldest first
    new_items = []
    for item in reversed(tweet_items):
        if item["url"] not in posted:
            new_items.append(item)

    if not new_items:
        print("No new tweets to post")
        return

    print(f"Found {len(new_items)} new tweet(s) to post")

    # Login to Bluesky
    bsky_client = Client()
    try:
        bsky_client.login(BSKY_HANDLE, bsky_password)
        print(f"Logged in to Bluesky as {BSKY_HANDLE}")
    except Exception as e:
        print(f"Error logging in to Bluesky: {e}")
        sys.exit(1)

    # Post each new item
    for i, item in enumerate(new_items):
        text = clean_tweet_text(item["text"])
        url = item["url"]
        post_text = format_post(text, url)

        print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")
        print(f"  Text: {post_text[:80]}...")

        try:
            rich_text = create_rich_post(bsky_client, post_text, url)
            bsky_client.send_post(rich_text)
            save_posted(url)
            print("  Posted successfully")
        except Exception as e:
            print(f"  Error posting: {e}")
            continue

        if i < len(new_items) - 1:
            print(f"  Waiting {POST_DELAY}s before next post...")
            time.sleep(POST_DELAY)

    print("\nDone!")


if __name__ == "__main__":
    main()
