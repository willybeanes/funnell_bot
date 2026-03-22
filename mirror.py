#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using a Nitter RSS feed.

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
from xml.etree import ElementTree

import httpx
from atproto import Client, client_utils, models

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
POSTED_MAP_FILE = Path(__file__).parent / "posted_map.json"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
FETCH_MAX_RETRIES = 3
FETCH_BACKOFF_BASE = 10  # seconds; will retry at 10s, 20s, 40s
TWITTER_USERNAME = "sportz_nutt51"

NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
    "https://nitter.perennialte.ch",
    "https://nitter.1d4.us",
]
# Allow env var override to prepend a preferred instance
_env_instance = os.environ.get("NITTER_INSTANCE")
if _env_instance:
    NITTER_INSTANCES = [_env_instance] + [u for u in NITTER_INSTANCES if u != _env_instance]
FXTWITTER_API = "https://api.fxtwitter.com"


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

def _extract_tweet_id_from_nitter_link(link: str) -> str | None:
    """Extract tweet ID from a Nitter RSS link like .../username/status/123456#m."""
    match = re.search(r"/status/(\d+)", link)
    return match.group(1) if match else None


def _enrich_with_fxtwitter(tweet: dict) -> dict:
    """Fetch quote-tweet and reply metadata from the FixTweet API.

    Populates quoted_text, quoted_user, and reply_to_tweet_id when available.
    Falls back silently so RSS data is still usable if the API is down.
    """
    tweet_id = tweet["id"]
    url = f"{FXTWITTER_API}/{TWITTER_USERNAME}/status/{tweet_id}"
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  FxTwitter API returned {resp.status_code} for tweet {tweet_id}")
            return tweet
        data = resp.json()
    except Exception as e:
        print(f"  FxTwitter enrichment failed for tweet {tweet_id}: {e}")
        return tweet

    status = data.get("tweet") or {}

    # Extract quote tweet
    quote = status.get("quote")
    if quote:
        tweet["quoted_text"] = quote.get("text")
        author = quote.get("author") or {}
        tweet["quoted_user"] = author.get("screen_name")

    # Extract reply-to info
    replying_to = status.get("replying_to")
    if replying_to and isinstance(replying_to, dict):
        parent_id = replying_to.get("post")
        if parent_id:
            tweet["reply_to_tweet_id"] = str(parent_id)
    elif replying_to and isinstance(replying_to, str):
        # Legacy format: replying_to is just the screen_name
        # Check replying_to_status for the tweet ID
        parent_id = status.get("replying_to_status")
        if parent_id:
            tweet["reply_to_tweet_id"] = str(parent_id)

    return tweet


def _try_fetch_rss(rss_url: str, headers: dict) -> tuple[str, httpx.Response | None]:
    """Try fetching RSS from a single instance with retry on 429.

    Returns (status, response) where status is one of:
    "ok", "rate_limited", "error"
    """
    resp = None
    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        try:
            resp = httpx.get(rss_url, headers=headers, timeout=30, follow_redirects=True)
            if resp.status_code == 429:
                wait = FETCH_BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"  Rate-limited (429). Retry {attempt}/{FETCH_MAX_RETRIES} in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # Verify we got XML, not an HTML error page
            content = resp.text.strip()
            if not content.startswith("<?xml") and not content.startswith("<rss") and not content.startswith("<feed"):
                print(f"  Response is not valid RSS/XML (starts with: {content[:50]!r})")
                return ("error", None)
            return ("ok", resp)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                wait = FETCH_BACKOFF_BASE * (2 ** (attempt - 1))
                print(f"  Rate-limited (429). Retry {attempt}/{FETCH_MAX_RETRIES} in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  HTTP error: {e}")
            return ("error", None)
        except Exception as e:
            print(f"  Request error: {e}")
            return ("error", None)

    return ("rate_limited", None)


def fetch_tweets() -> list[dict] | None:
    """Fetch recent tweets via Nitter RSS, trying multiple instances.

    Cycles through NITTER_INSTANCES until one returns valid RSS.
    Retries with exponential backoff on 429 (rate limit) responses.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    resp = None
    all_rate_limited = True
    for instance in NITTER_INSTANCES:
        rss_url = f"{instance}/{TWITTER_USERNAME}/rss"
        print(f"Trying Nitter instance: {instance}")
        status, resp = _try_fetch_rss(rss_url, headers)
        if status == "ok":
            all_rate_limited = False
            break
        elif status == "error":
            all_rate_limited = False
            print(f"  Instance {instance} failed, trying next...")
            continue
        else:  # rate_limited
            print(f"  Instance {instance} rate-limited, trying next...")
            continue

    if resp is None:
        if all_rate_limited:
            print("Error: All Nitter instances are rate-limited")
            return "rate_limited"
        print("Error: All Nitter instances failed")
        return None

    try:
        root = ElementTree.fromstring(resp.text)
    except ElementTree.ParseError as e:
        print(f"Error parsing RSS XML: {e}")
        return None

    channel = root.find("channel")
    if channel is None:
        print("Error: No <channel> in RSS feed")
        return None

    DC_NS = "http://purl.org/dc/elements/1.1/"
    items = channel.findall("item")

    results = []
    for item in items:
        link = (item.findtext("link") or "").strip()
        tweet_id = _extract_tweet_id_from_nitter_link(link)
        if not tweet_id:
            continue

        # The <title> holds the plain-text tweet content
        text = (item.findtext("title") or "").strip()
        if not text:
            continue

        # Determine author — dc:creator gives "@username"
        creator = (item.findtext(f"{{{DC_NS}}}creator") or "").strip().lstrip("@")
        screen_name = creator or TWITTER_USERNAME
        tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}"

        # Nitter RSS doesn't include structured quote-tweet or reply metadata,
        # but we can detect retweets (title starts with "RT by @user")
        # and self-replies won't be available via RSS.
        results.append({
            "text": text,
            "url": tweet_url,
            "id": str(tweet_id),
            "quoted_text": None,
            "quoted_user": None,
            "reply_to_tweet_id": None,
        })

    print(f"Fetched {len(results)} tweets from @{TWITTER_USERNAME} via Nitter RSS")
    if not results:
        return None

    # Enrich each tweet with quote-tweet and reply data from FixTweet API
    print("Enriching tweets with quote/reply data via FixTweet API...")
    for i, tweet in enumerate(results):
        results[i] = _enrich_with_fxtwitter(tweet)
    enriched = sum(1 for t in results if t.get("quoted_text") or t.get("reply_to_tweet_id"))
    if enriched:
        print(f"  Enriched {enriched} tweet(s) with quote/reply metadata")

    return results


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
    if tweet_items is None or tweet_items == "rate_limited":
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
    if tweet_items == "rate_limited":
        print("Skipping this run due to rate limiting. Will retry next scheduled run.")
        return
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
