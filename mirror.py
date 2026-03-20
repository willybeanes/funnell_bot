#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot via Nitter RSS feeds."""

import os
import re
import sys
import time
from pathlib import Path

import feedparser
from atproto import Client, client_utils

FEED_URLS = [
    "https://xcancel.com/sportz_nutt51/rss",
    "https://nitter.poast.org/sportz_nutt51/rss",
]

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


def fetch_feed() -> list[dict] | None:
    for feed_url in FEED_URLS:
        print(f"Trying feed: {feed_url}")
        feed = feedparser.parse(feed_url)
        if feed.bozo and not feed.entries:
            print(f"  Feed error: {feed.bozo_exception}")
            continue
        if feed.entries:
            print(f"  Found {len(feed.entries)} entries")
            return feed.entries
        print("  No entries found")
    return None


def clean_tweet_text(text: str) -> str:
    # Strip leading "RT @username: " prefix
    text = re.sub(r"^RT @\w+:\s*", "", text)
    # Clean up HTML entities that feedparser might leave
    text = text.strip()
    return text


def normalize_tweet_url(url: str) -> str:
    """Convert any nitter/xcancel URL to canonical x.com URL."""
    match = re.search(r"/status/(\d+)", url)
    if match:
        return f"https://x.com/{TWITTER_USERNAME}/status/{match.group(1)}"
    return url


def format_post(text: str, tweet_url: str) -> str:
    suffix = f"\n\n🐦 {tweet_url}"
    max_text_len = BSKY_CHAR_LIMIT - len(suffix)

    if len(text) + len(suffix) > BSKY_CHAR_LIMIT:
        truncation_marker = f"… [full tweet: {tweet_url}]"
        max_text_len = BSKY_CHAR_LIMIT - len(suffix) - len(truncation_marker) + len(text[:0])
        # Recalculate: we need text + truncation_marker + suffix <= 300
        available = BSKY_CHAR_LIMIT - len(truncation_marker) - len(suffix)
        text = text[:available] + truncation_marker

    return text + suffix


def create_rich_post(client: Client, post_text: str, tweet_url: str):
    """Create a post with a clickable link facet for the tweet URL."""
    tb = client_utils.TextBuilder()

    # Find where the URL is in the post text
    url_start = post_text.find(tweet_url)
    if url_start == -1:
        # URL not found as plain text, just post as-is
        tb.text(post_text)
    else:
        # Add text before the URL
        tb.text(post_text[:url_start])
        # Add the URL as a clickable link
        tb.link(tweet_url, tweet_url)
        # Add any text after the URL
        remaining = post_text[url_start + len(tweet_url):]
        if remaining:
            tb.text(remaining)

    return tb


def main():
    password = os.environ.get("BSKY_APP_PASSWORD")
    if not password:
        print("Error: BSKY_APP_PASSWORD environment variable not set")
        sys.exit(1)

    # Fetch RSS feed
    entries = fetch_feed()
    if entries is None:
        print("Error: Could not fetch RSS feed from any source")
        sys.exit(1)

    posted = load_posted()

    # Collect new items, oldest first
    new_items = []
    for entry in reversed(entries):
        url = normalize_tweet_url(entry.link)
        if url not in posted:
            new_items.append((url, entry))

    if not new_items:
        print("No new tweets to post")
        return

    print(f"Found {len(new_items)} new tweet(s) to post")

    # Login to Bluesky
    client = Client()
    try:
        client.login(BSKY_HANDLE, password)
        print(f"Logged in to Bluesky as {BSKY_HANDLE}")
    except Exception as e:
        print(f"Error logging in to Bluesky: {e}")
        sys.exit(1)

    # Post each new item
    for i, (url, entry) in enumerate(new_items):
        text = clean_tweet_text(entry.title if entry.title else "")
        post_text = format_post(text, url)

        print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")
        print(f"  Text: {post_text[:80]}...")

        try:
            rich_text = create_rich_post(client, post_text, url)
            client.send_post(rich_text)
            save_posted(url)
            print("  Posted successfully")
        except Exception as e:
            print(f"  Error posting: {e}")
            continue

        # Delay between posts to avoid rate limits
        if i < len(new_items) - 1:
            print(f"  Waiting {POST_DELAY}s before next post...")
            time.sleep(POST_DELAY)

    print("\nDone!")


if __name__ == "__main__":
    main()
