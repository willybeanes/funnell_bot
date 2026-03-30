#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using twscrape for tweet fetching.

Supports:
- Quote tweets (inline quoted text)
- Thread detection (self-replies become Bluesky reply chains)
- Retweet detection (skipped or formatted with attribution)
- Duplicate prevention via posted_map.json
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from atproto import Client, client_utils, models
from twscrape import API, gather

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
POSTED_MAP_FILE = Path(__file__).parent / "posted_map.json"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
TWITTER_USERNAME = "sportz_nutt51"


# --- State tracking ---

def load_posted_map() -> dict:
    """Load tweet_id -> {uri, cid} mapping for thread support."""
    if POSTED_MAP_FILE.exists():
        try:
            return json.loads(POSTED_MAP_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_posted_map(posted_map: dict) -> None:
    POSTED_MAP_FILE.write_text(json.dumps(posted_map, indent=2) + "\n")


def load_posted_urls() -> set[str]:
    """Load legacy posted.txt for backward compat."""
    if not POSTED_FILE.exists():
        return set()
    return set(POSTED_FILE.read_text().strip().splitlines())


def is_posted(tweet_id: str, posted_map: dict, posted_urls: set[str]) -> bool:
    """Check if a tweet has been posted (in either tracking file)."""
    if tweet_id in posted_map:
        return True
    url = f"https://x.com/{TWITTER_USERNAME}/status/{tweet_id}"
    return url in posted_urls


def record_posted(tweet_id: str, tweet_url: str, bsky_uri: str, bsky_cid: str,
                  posted_map: dict) -> None:
    """Record a posted tweet in both tracking files."""
    posted_map[tweet_id] = {"uri": bsky_uri, "cid": bsky_cid}
    save_posted_map(posted_map)
    with open(POSTED_FILE, "a") as f:
        f.write(tweet_url + "\n")


# --- Tweet fetching via twscrape ---

async def fetch_tweets() -> list[dict] | None:
    """Fetch recent tweets using twscrape (Twitter's internal GraphQL API)."""
    username = os.environ.get("TWITTER_USERNAME", "")
    password = os.environ.get("TWITTER_PASSWORD", "")
    email = os.environ.get("TWITTER_EMAIL", "")
    cookies = os.environ.get("TWITTER_COOKIES", "")

    if not cookies and not password:
        print("Error: TWITTER_COOKIES or TWITTER_PASSWORD environment variable required")
        return None

    db_path = Path(__file__).parent / "accounts.db"
    api = API(str(db_path))

    # Add account if not already present
    try:
        if cookies:
            await api.pool.add_account(
                username, password or "", email or "", "",
                cookies=cookies,
            )
        else:
            await api.pool.add_account(username, password, email, "")
            await api.pool.login_all()
        print(f"Twitter account '{username}' ready")
    except Exception as e:
        # Account may already be in the pool from a previous run
        err_msg = str(e).lower()
        if "unique" in err_msg or "already" in err_msg:
            print(f"Twitter account '{username}' already in pool")
        else:
            print(f"Warning adding account: {e}")

    # Resolve username to numeric ID
    try:
        user = await api.user_by_login(TWITTER_USERNAME)
        user_id = user.id
        print(f"Resolved @{TWITTER_USERNAME} -> ID {user_id}")
    except Exception as e:
        print(f"Error resolving @{TWITTER_USERNAME}: {e}")
        return None

    # Fetch recent tweets
    try:
        tweets = await gather(api.user_tweets(user_id, limit=20))
        print(f"Fetched {len(tweets)} tweets from @{TWITTER_USERNAME}")
    except Exception as e:
        print(f"Error fetching tweets: {e}")
        return None

    results = []
    for tweet in tweets:
        tweet_id = str(tweet.id)
        text = tweet.rawContent or ""
        tweet_url = f"https://x.com/{TWITTER_USERNAME}/status/{tweet_id}"

        # Skip pure retweets (we only want original content)
        if tweet.retweetedTweet is not None:
            continue

        # Quote tweet data
        quoted_text = None
        quoted_user = None
        if tweet.quotedTweet is not None:
            quoted_text = tweet.quotedTweet.rawContent or ""
            quoted_user = tweet.quotedTweet.user.username if tweet.quotedTweet.user else None

        # Thread detection: self-reply
        reply_to_tweet_id = None
        if tweet.inReplyToTweetId is not None:
            # Check if it's a self-reply (thread) vs reply to someone else
            if tweet.inReplyToUser and tweet.inReplyToUser.username.lower() == TWITTER_USERNAME.lower():
                reply_to_tweet_id = str(tweet.inReplyToTweetId)
            else:
                # Reply to someone else — skip
                continue

        results.append({
            "text": text,
            "url": tweet_url,
            "id": tweet_id,
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
            "reply_to_tweet_id": reply_to_tweet_id,
        })

    print(f"  {len(results)} original tweets/threads (skipped retweets and replies to others)")
    return results if results else None


# --- Bluesky posting ---

def clean_tweet_text(text: str) -> str:
    text = re.sub(r"^RT @\w+:\s*", "", text)
    # Remove t.co links at the end (Twitter appends these for quote tweets)
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text)
    return text.strip()


def format_post(text: str, tweet_url: str,
                quoted_text: str | None = None,
                quoted_user: str | None = None) -> str:
    if quoted_text:
        quote_label = f"@{quoted_user}" if quoted_user else "quoted"
        text = f"{text}\n\n💬 {quote_label}: {quoted_text}"

    suffix = f"\n\n🐦 {tweet_url}"

    if len(text) + len(suffix) > BSKY_CHAR_LIMIT:
        available = BSKY_CHAR_LIMIT - len(suffix) - 1  # -1 for the …
        text = text[:available] + "…"

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


def build_reply_ref(posted_map: dict, reply_to_tweet_id: str, all_items: list[dict]):
    """Build the Bluesky reply reference for a thread reply."""
    parent_id = reply_to_tweet_id

    if parent_id not in posted_map:
        return None

    parent_uri = posted_map[parent_id]["uri"]
    parent_cid = posted_map[parent_id]["cid"]

    if not parent_uri or not parent_cid:
        return None

    # Walk back to find the thread root
    reply_chain = {}
    for item in all_items:
        if item.get("reply_to_tweet_id"):
            reply_chain[item["id"]] = item["reply_to_tweet_id"]

    root_id = parent_id
    visited = set()
    current = root_id
    while current not in visited:
        visited.add(current)
        found_parent = None
        for item in all_items:
            if item["id"] == current and item.get("reply_to_tweet_id"):
                found_parent = item["reply_to_tweet_id"]
                break
        if found_parent and found_parent in posted_map:
            current = found_parent
        else:
            break

    root_id = current
    if root_id in posted_map and posted_map[root_id]["uri"]:
        root_uri = posted_map[root_id]["uri"]
        root_cid = posted_map[root_id]["cid"]
    else:
        root_uri = parent_uri
        root_cid = parent_cid

    return models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )


# --- Commands ---

def seed():
    """Mark all current tweets as already posted, so only future tweets get mirrored."""
    tweet_items = asyncio.run(fetch_tweets())
    if tweet_items is None:
        print("Error: Could not fetch tweets for seeding")
        sys.exit(1)

    posted_map = load_posted_map()
    posted_urls = load_posted_urls()
    count = 0
    for item in tweet_items:
        if not is_posted(item["id"], posted_map, posted_urls):
            posted_map[item["id"]] = {"uri": "", "cid": ""}
            with open(POSTED_FILE, "a") as f:
                f.write(item["url"] + "\n")
            count += 1

    save_posted_map(posted_map)
    print(f"Seeded {count} existing tweets (total tracked: {len(posted_map)})")
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
    tweet_items = asyncio.run(fetch_tweets())
    if tweet_items is None:
        print("Error: Could not fetch tweets")
        sys.exit(1)

    posted_map = load_posted_map()
    posted_urls = load_posted_urls()

    # Collect new items, oldest first
    new_items = []
    for item in reversed(tweet_items):
        if not is_posted(item["id"], posted_map, posted_urls):
            new_items.append(item)

    if not new_items:
        print("No new tweets to post")
        return

    print(f"Found {len(new_items)} new tweet(s) to post")

    thread_count = sum(1 for item in new_items if item.get("reply_to_tweet_id"))
    if thread_count:
        print(f"  ({thread_count} are thread replies)")

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

        # Check if this is a thread reply
        reply_ref = None
        reply_to = item.get("reply_to_tweet_id")
        if reply_to:
            reply_ref = build_reply_ref(posted_map, reply_to, new_items + tweet_items)
            if reply_ref:
                print(f"\nPosting thread reply ({i + 1}/{len(new_items)}): {url}")
                print(f"  ↳ replying to tweet {reply_to}")
            else:
                print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")
                print(f"  (thread parent {reply_to} not found, posting standalone)")
        else:
            print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")

        print(f"  Text: {post_text[:80]}...")

        try:
            rich_text = create_rich_post(bsky_client, post_text, url)
            if reply_ref:
                response = bsky_client.send_post(rich_text, reply_to=reply_ref)
            else:
                response = bsky_client.send_post(rich_text)

            record_posted(
                item["id"], url,
                response.uri, response.cid,
                posted_map
            )
            print("  Posted successfully")
        except Exception as e:
            print(f"  Error posting: {e}")
            posted_map[item["id"]] = {"uri": "", "cid": ""}
            save_posted_map(posted_map)
            with open(POSTED_FILE, "a") as f:
                f.write(url + "\n")
            continue

        if i < len(new_items) - 1:
            print(f"  Waiting {POST_DELAY}s before next post...")
            time.sleep(POST_DELAY)

    print("\nDone!")


if __name__ == "__main__":
    main()
