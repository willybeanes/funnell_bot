#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twitter's syndication endpoint."""

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from atproto import Client, client_utils
from bs4 import BeautifulSoup

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
TWITTER_USERNAME = "sportz_nutt51"

SYNDICATION_URL = (
    f"https://syndication.twitter.com/srv/timeline-profile/"
    f"screen-name/{TWITTER_USERNAME}"
)


def load_posted() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    return set(POSTED_FILE.read_text().strip().splitlines())


def save_posted(url: str) -> None:
    with open(POSTED_FILE, "a") as f:
        f.write(url + "\n")


def fetch_tweets() -> list[dict] | None:
    """Fetch recent tweets via Twitter's syndication/embed endpoint."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    try:
        resp = httpx.get(SYNDICATION_URL, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        print(f"Error fetching syndication page: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Tweet data is embedded in a script tag as JSON props
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script:
        print("Error: Could not find tweet data in syndication page")
        print(f"Page length: {len(resp.text)} chars")
        return None

    try:
        data = json.loads(script.string)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return None

    # Navigate the JSON structure to find tweets
    try:
        timeline = data["props"]["pageProps"]["timeline"]
        entries = timeline.get("entries", [])
    except (KeyError, TypeError) as e:
        print(f"Error navigating tweet data: {e}")
        return None

    results = []
    for entry in entries:
        content = entry.get("content", {})
        tweet = content.get("tweet", content)

        tweet_id = tweet.get("id_str") or tweet.get("id")
        text = tweet.get("text", "")

        if not tweet_id or not text:
            continue

        # Get the screen name from the tweet's user or fall back to configured username
        screen_name = TWITTER_USERNAME
        user_data = tweet.get("user", {})
        if user_data.get("screen_name"):
            screen_name = user_data["screen_name"]

        tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

        # Extract quoted tweet text if present
        quoted_text = None
        quoted_user = None
        if tweet.get("is_quote_status") and tweet.get("quoted_status"):
            qs = tweet["quoted_status"]
            quoted_text = qs.get("full_text") or qs.get("text", "")
            qs_user = qs.get("user", {})
            quoted_user = qs_user.get("screen_name")

        results.append({
            "text": text,
            "url": tweet_url,
            "id": str(tweet_id),
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
        })

    print(f"Fetched {len(results)} tweets from @{TWITTER_USERNAME}")
    return results if results else None


def clean_tweet_text(text: str) -> str:
    # Strip leading "RT @username: " prefix
    text = re.sub(r"^RT @\w+:\s*", "", text)
    # Expand t.co links would require extra requests, so just leave them
    return text.strip()


def format_post(text: str, tweet_url: str, quoted_text: str | None = None, quoted_user: str | None = None) -> str:
    # Append quoted tweet if present
    if quoted_text:
        quote_label = f"@{quoted_user}" if quoted_user else "quoted"
        text = f"{text}\n\n💬 {quote_label}: {quoted_text}"

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


def seed():
    """Mark all current tweets as already posted, so only future tweets get mirrored."""
    tweet_items = fetch_tweets()
    if tweet_items is None:
        print("Error: Could not fetch tweets for seeding")
        sys.exit(1)

    posted = load_posted()
    count = 0
    for item in tweet_items:
        if item["url"] not in posted:
            save_posted(item["url"])
            count += 1

    print(f"Seeded {count} existing tweets into posted.txt (total: {count + len(posted)})")
    print("Only new tweets from this point forward will be mirrored.")


def main():
    if "--seed" in sys.argv:
        seed()
        return

    bsky_password = os.environ.get("BSKY_APP_PASSWORD")
    if not bsky_password:
        print("Error: BSKY_APP_PASSWORD environment variable not set")
        sys.exit(1)

    # Fetch tweets
    tweet_items = fetch_tweets()
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
        quoted_text = item.get("quoted_text")
        if quoted_text:
            quoted_text = clean_tweet_text(quoted_text)
        post_text = format_post(text, url, quoted_text, item.get("quoted_user"))

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
