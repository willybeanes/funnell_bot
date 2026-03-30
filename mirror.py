#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twitter's syndication endpoint.

Supports:
- Quote tweets (inline quoted text)
- Thread detection (self-replies become Bluesky reply chains)
- Duplicate prevention via posted_map.json
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from atproto import Client, client_utils, models
from bs4 import BeautifulSoup

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
POSTED_MAP_FILE = Path(__file__).parent / "posted_map.json"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
TWITTER_USERNAME = "sportz_nutt51"

SYNDICATION_URL = (
    f"https://syndication.twitter.com/srv/timeline-profile/"
    f"screen-name/{TWITTER_USERNAME}"
)


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
    # Check legacy posted.txt by URL pattern
    url = f"https://x.com/{TWITTER_USERNAME}/status/{tweet_id}"
    return url in posted_urls


def record_posted(tweet_id: str, tweet_url: str, bsky_uri: str, bsky_cid: str,
                  posted_map: dict) -> None:
    """Record a posted tweet in both tracking files."""
    posted_map[tweet_id] = {"uri": bsky_uri, "cid": bsky_cid}
    save_posted_map(posted_map)
    # Also append to posted.txt for backward compat
    with open(POSTED_FILE, "a") as f:
        f.write(tweet_url + "\n")


# --- Tweet fetching ---

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

    try:
        timeline = data["props"]["pageProps"]["timeline"]
        entries = timeline.get("entries", [])
    except (KeyError, TypeError) as e:
        print(f"Error navigating tweet data: {e}")
        return None

    # Determine the user's own ID for self-reply detection
    user_id = None
    for entry in entries:
        content = entry.get("content", {})
        tweet = content.get("tweet", content)
        if tweet.get("user", {}).get("screen_name", "").lower() == TWITTER_USERNAME.lower():
            user_id = tweet["user"].get("id_str")
            break

    results = []
    for entry in entries:
        content = entry.get("content", {})
        tweet = content.get("tweet", content)

        tweet_id = tweet.get("id_str") or tweet.get("id")
        text = tweet.get("text", "")

        if not tweet_id or not text:
            continue

        screen_name = TWITTER_USERNAME
        user_data = tweet.get("user", {})
        if user_data.get("screen_name"):
            screen_name = user_data["screen_name"]

        tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

        # Quote tweet data
        quoted_text = None
        quoted_user = None
        if tweet.get("is_quote_status") and tweet.get("quoted_status"):
            qs = tweet["quoted_status"]
            quoted_text = qs.get("full_text") or qs.get("text", "")
            qs_user = qs.get("user", {})
            quoted_user = qs_user.get("screen_name")

        # Thread detection: is this a self-reply?
        reply_to_tweet_id = None
        reply_to_user = tweet.get("in_reply_to_user_id_str")
        if reply_to_user and reply_to_user == user_id:
            reply_to_tweet_id = tweet.get("in_reply_to_status_id_str")

        results.append({
            "text": text,
            "url": tweet_url,
            "id": str(tweet_id),
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
            "reply_to_tweet_id": reply_to_tweet_id,
        })

    print(f"Fetched {len(results)} tweets from @{TWITTER_USERNAME}")
    return results if results else None


# --- Bluesky posting ---

def clean_tweet_text(text: str) -> str:
    text = re.sub(r"^RT @\w+:\s*", "", text)
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


def build_reply_ref(posted_map: dict, reply_to_tweet_id: str, new_items: list[dict]):
    """Build the Bluesky reply reference for a thread reply.

    Walks back the self-reply chain to find the root post, then returns
    a ReplyRef with root and parent references.
    """
    parent_id = reply_to_tweet_id

    if parent_id not in posted_map:
        return None

    parent_uri = posted_map[parent_id]["uri"]
    parent_cid = posted_map[parent_id]["cid"]

    # Walk back to find the thread root
    root_id = parent_id
    # Build a lookup of reply chains from fetched tweets
    reply_chain = {}
    for item in new_items:
        if item.get("reply_to_tweet_id"):
            reply_chain[item["id"]] = item["reply_to_tweet_id"]

    # Walk up the chain to find root
    visited = set()
    current = root_id
    while current in reply_chain and current not in visited:
        visited.add(current)
        current = reply_chain[current]
    # Also check posted_map for earlier thread parts
    # The root is the first post in the chain that isn't a reply to another posted tweet
    # For simplicity, walk up through posted_map
    visited.clear()
    current = root_id
    while current not in visited:
        visited.add(current)
        # Check if this tweet is itself a reply to another posted tweet
        # We need to look at the fetched data
        found_parent = None
        for item in new_items:
            if item["id"] == current and item.get("reply_to_tweet_id"):
                found_parent = item["reply_to_tweet_id"]
                break
        if found_parent and found_parent in posted_map:
            current = found_parent
        else:
            break

    root_id = current
    if root_id in posted_map:
        root_uri = posted_map[root_id]["uri"]
        root_cid = posted_map[root_id]["cid"]
    else:
        # Root not in our records, use parent as root
        root_uri = parent_uri
        root_cid = parent_cid

    return models.AppBskyFeedPost.ReplyRef(
        root=models.ComAtprotoRepoStrongRef.Main(uri=root_uri, cid=root_cid),
        parent=models.ComAtprotoRepoStrongRef.Main(uri=parent_uri, cid=parent_cid),
    )


# --- Commands ---

def seed():
    """Mark all current tweets as already posted, so only future tweets get mirrored."""
    tweet_items = fetch_tweets()
    if tweet_items is None:
        print("Error: Could not fetch tweets for seeding")
        sys.exit(1)

    posted_map = load_posted_map()
    posted_urls = load_posted_urls()
    count = 0
    for item in tweet_items:
        if not is_posted(item["id"], posted_map, posted_urls):
            # Seed with empty uri/cid since we didn't actually post these
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
    tweet_items = fetch_tweets()
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

    # Show thread info
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

            # Record the mapping for future thread replies
            record_posted(
                item["id"], url,
                response.uri, response.cid,
                posted_map
            )
            print("  Posted successfully")
        except Exception as e:
            print(f"  Error posting: {e}")
            # Still record as posted to avoid retrying broken tweets
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
