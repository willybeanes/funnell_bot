#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twitter's GraphQL API.

Uses a guest token (no auth required) to fetch tweets via the same
GraphQL endpoints that twitter.com uses internally. Falls back to
cookie-based auth if guest token has limited access.

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
from datetime import datetime, timezone
from pathlib import Path

import httpx
from atproto import Client, client_utils, models

BSKY_HANDLE = "sportz-nutt51-bot.bsky.social"
POSTED_FILE = Path(__file__).parent / "posted.txt"
POSTED_MAP_FILE = Path(__file__).parent / "posted_map.json"
BSKY_CHAR_LIMIT = 300
POST_DELAY = 5
TWITTER_USERNAME = "sportz_nutt51"

# Twitter's public bearer token (same one the web app uses)
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

GRAPHQL_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


# --- State tracking ---

def load_posted_map() -> dict:
    if POSTED_MAP_FILE.exists():
        try:
            return json.loads(POSTED_MAP_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_posted_map(posted_map: dict) -> None:
    POSTED_MAP_FILE.write_text(json.dumps(posted_map, indent=2) + "\n")


def load_posted_urls() -> set[str]:
    if not POSTED_FILE.exists():
        return set()
    return set(POSTED_FILE.read_text().strip().splitlines())


def is_posted(tweet_id: str, posted_map: dict, posted_urls: set[str]) -> bool:
    if tweet_id in posted_map:
        return True
    url = f"https://x.com/{TWITTER_USERNAME}/status/{tweet_id}"
    return url in posted_urls


def record_posted(tweet_id: str, tweet_url: str, bsky_uri: str, bsky_cid: str,
                  posted_map: dict) -> None:
    posted_map[tweet_id] = {"uri": bsky_uri, "cid": bsky_cid}
    save_posted_map(posted_map)
    with open(POSTED_FILE, "a") as f:
        f.write(tweet_url + "\n")


# --- Twitter GraphQL client ---

class TwitterClient:
    """Minimal Twitter GraphQL client using guest token or cookies."""

    def __init__(self):
        self.client = httpx.Client(timeout=15)
        self.headers = {
            "authorization": f"Bearer {BEARER_TOKEN}",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        self.use_cookies = False

    def activate_guest(self) -> bool:
        """Get a guest token for unauthenticated access."""
        try:
            resp = self.client.post(
                "https://api.twitter.com/1.1/guest/activate.json",
                headers=self.headers,
            )
            if resp.status_code == 200:
                token = resp.json().get("guest_token")
                if token:
                    self.headers["x-guest-token"] = token
                    print(f"Activated guest token")
                    return True
        except Exception as e:
            print(f"Error getting guest token: {e}")
        return False

    def activate_cookies(self, cookies_str: str) -> bool:
        """Use browser cookies for authenticated access."""
        try:
            cookie_dict = {}
            for part in cookies_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

            if "ct0" in cookie_dict:
                self.headers["x-csrf-token"] = cookie_dict["ct0"]
            # Remove guest token if present
            self.headers.pop("x-guest-token", None)

            self.client = httpx.Client(
                timeout=15,
                cookies=cookie_dict,
            )
            self.use_cookies = True
            print("Using cookie-based auth")
            return True
        except Exception as e:
            print(f"Error setting up cookies: {e}")
            return False

    def _graphql_get(self, endpoint: str, variables: dict) -> dict | None:
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(GRAPHQL_FEATURES),
        }
        try:
            resp = self.client.get(
                f"https://twitter.com/i/api/graphql/{endpoint}",
                params=params,
                headers=self.headers,
            )
            if resp.status_code == 200:
                return resp.json()
            print(f"  GraphQL {endpoint} returned {resp.status_code}")
            if resp.status_code == 429:
                print("  Rate limited — try again later")
            return None
        except Exception as e:
            print(f"  GraphQL request error: {e}")
            return None

    def get_user_id(self, screen_name: str) -> str | None:
        data = self._graphql_get(
            "xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True},
        )
        if data:
            try:
                return data["data"]["user"]["result"]["rest_id"]
            except (KeyError, TypeError):
                pass
        return None

    def get_user_tweets(self, user_id: str, count: int = 20) -> list[dict] | None:
        data = self._graphql_get(
            "XicnWRbyQ3WgVY__VataBQ/UserTweets",
            {
                "userId": user_id,
                "count": count,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": False,
                "withVoice": False,
                "withV2Timeline": True,
            },
        )
        if not data:
            return None
        return self._parse_timeline(data, user_id)

    def _parse_timeline(self, data: dict, user_id: str) -> list[dict]:
        results = []
        try:
            instructions = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
        except (KeyError, TypeError):
            return results

        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                tweet = self._parse_entry(entry, user_id)
                if tweet:
                    results.append(tweet)

        return results

    def _parse_entry(self, entry: dict, user_id: str) -> dict | None:
        content = entry.get("content", {})
        if content.get("entryType") != "TimelineTimelineItem":
            return None

        tweet_result = (
            content.get("itemContent", {})
            .get("tweet_results", {})
            .get("result", {})
        )

        # Handle tweets wrapped in "tweet" key (for promoted/tombstone)
        if "tweet" in tweet_result:
            tweet_result = tweet_result["tweet"]

        legacy = tweet_result.get("legacy", {})
        tweet_id = legacy.get("id_str")
        text = legacy.get("full_text", "")

        if not tweet_id or not text:
            return None

        # Get author info
        core = tweet_result.get("core", {})
        user_results = core.get("user_results", {}).get("result", {})
        author_id = user_results.get("rest_id", "")
        author_screen_name = user_results.get("legacy", {}).get("screen_name", TWITTER_USERNAME)

        tweet_url = f"https://x.com/{author_screen_name}/status/{tweet_id}"

        # Skip retweets
        rt = legacy.get("retweeted_status_result")
        if rt:
            return None

        # Quote tweet data
        quoted_text = None
        quoted_user = None
        qt_result = tweet_result.get("quoted_status_result", {}).get("result", {})
        if qt_result:
            qt_legacy = qt_result.get("legacy", {})
            quoted_text = qt_legacy.get("full_text", "")
            qt_user = qt_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
            quoted_user = qt_user.get("screen_name")

        # Parse timestamp
        created_at = legacy.get("created_at", "")
        tweet_time = None
        if created_at:
            try:
                # Twitter format: "Thu Mar 30 15:42:00 +0000 2026"
                tweet_time = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
            except ValueError:
                pass

        # Thread detection: self-reply
        reply_to_tweet_id = None
        reply_to_user_id = legacy.get("in_reply_to_user_id_str")
        if reply_to_user_id:
            if reply_to_user_id == user_id:
                reply_to_tweet_id = legacy.get("in_reply_to_status_id_str")
            else:
                # Reply to someone else — skip
                return None

        return {
            "text": text,
            "url": tweet_url,
            "id": tweet_id,
            "created_at": tweet_time,
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
            "reply_to_tweet_id": reply_to_tweet_id,
        }


# --- Tweet fetching ---

def fetch_tweets() -> list[dict] | None:
    """Fetch recent tweets, trying guest token first, then cookies."""
    tc = TwitterClient()

    cookies = os.environ.get("TWITTER_COOKIES", "")

    # Strategy 1: Try cookies first (most reliable for recent tweets)
    if cookies:
        if tc.activate_cookies(cookies):
            user_id = tc.get_user_id(TWITTER_USERNAME)
            if user_id:
                print(f"Resolved @{TWITTER_USERNAME} -> ID {user_id}")
                tweets = tc.get_user_tweets(user_id, count=20)
                if tweets:
                    print(f"Fetched {len(tweets)} tweets (cookie auth)")
                    return tweets
                print("  Cookie auth returned no tweets")
            else:
                print("  Could not resolve user with cookies")

    # Strategy 2: Guest token (no auth needed, may have limited access)
    tc2 = TwitterClient()
    if tc2.activate_guest():
        user_id = tc2.get_user_id(TWITTER_USERNAME)
        if user_id:
            print(f"Resolved @{TWITTER_USERNAME} -> ID {user_id}")
            tweets = tc2.get_user_tweets(user_id, count=20)
            if tweets:
                print(f"Fetched {len(tweets)} tweets (guest token)")
                return tweets
            print("  Guest token returned no tweets")

    print("Error: Could not fetch tweets from any method")
    return None


# --- Bluesky posting ---

def clean_tweet_text(text: str) -> str:
    text = re.sub(r"^RT @\w+:\s*", "", text)
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
        available = BSKY_CHAR_LIMIT - len(suffix) - 1
        text = text[:available] + "…"

    return text + suffix


def create_rich_post(client: Client, post_text: str, tweet_url: str):
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
    parent_id = reply_to_tweet_id
    if parent_id not in posted_map:
        return None
    parent_uri = posted_map[parent_id]["uri"]
    parent_cid = posted_map[parent_id]["cid"]
    if not parent_uri or not parent_cid:
        return None

    # Walk back to find thread root
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
        if found_parent and found_parent in posted_map and posted_map[found_parent]["uri"]:
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
    tweet_items = fetch_tweets()
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


def main():
    if "--seed" in sys.argv:
        seed()
        return

    bsky_password = os.environ.get("BSKY_APP_PASSWORD")
    if not bsky_password:
        print("Error: BSKY_APP_PASSWORD environment variable not set")
        sys.exit(1)

    tweet_items = fetch_tweets()
    if tweet_items is None:
        print("Error: Could not fetch tweets")
        sys.exit(1)

    posted_map = load_posted_map()
    posted_urls = load_posted_urls()

    new_items = []
    for item in reversed(tweet_items):
        if not is_posted(item["id"], posted_map, posted_urls):
            new_items.append(item)

    if not new_items:
        print("No new tweets to post")
        return

    print(f"Found {len(new_items)} new tweet(s) to post")

    bsky_client = Client()
    try:
        bsky_client.login(BSKY_HANDLE, bsky_password)
        print(f"Logged in to Bluesky as {BSKY_HANDLE}")
    except Exception as e:
        print(f"Error logging in to Bluesky: {e}")
        sys.exit(1)

    # Calculate realistic delays between posts based on tweet timestamps
    MAX_TOTAL_DELAY = 13 * 60  # 13 min cap so we finish before next cron run
    MIN_DELAY = 5  # minimum seconds between posts

    delays = []
    for i in range(1, len(new_items)):
        prev_time = new_items[i - 1].get("created_at")
        curr_time = new_items[i].get("created_at")
        if prev_time and curr_time:
            prev_dt = datetime.fromisoformat(prev_time)
            curr_dt = datetime.fromisoformat(curr_time)
            gap = (curr_dt - prev_dt).total_seconds()
            delays.append(max(gap, MIN_DELAY))
        else:
            delays.append(MIN_DELAY)

    # Scale delays down if total exceeds our cap
    total_delay = sum(delays)
    if total_delay > MAX_TOTAL_DELAY and delays:
        scale = MAX_TOTAL_DELAY / total_delay
        delays = [d * scale for d in delays]
        print(f"Scaled delays to fit in {MAX_TOTAL_DELAY // 60}m (original total: {total_delay / 60:.1f}m)")

    for i, item in enumerate(new_items):
        text = clean_tweet_text(item["text"])
        url = item["url"]
        quoted_text = item.get("quoted_text")
        if quoted_text:
            quoted_text = clean_tweet_text(quoted_text)
        post_text = format_post(text, url, quoted_text, item.get("quoted_user"))

        reply_ref = None
        reply_to = item.get("reply_to_tweet_id")
        if reply_to:
            reply_ref = build_reply_ref(posted_map, reply_to, new_items + tweet_items)
            if reply_ref:
                print(f"\nPosting thread reply ({i + 1}/{len(new_items)}): {url}")
            else:
                print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")
        else:
            print(f"\nPosting ({i + 1}/{len(new_items)}): {url}")

        print(f"  Text: {post_text[:80]}...")

        try:
            rich_text = create_rich_post(bsky_client, post_text, url)
            if reply_ref:
                response = bsky_client.send_post(rich_text, reply_to=reply_ref)
            else:
                response = bsky_client.send_post(rich_text)
            record_posted(item["id"], url, response.uri, response.cid, posted_map)
            print("  Posted successfully")
        except Exception as e:
            print(f"  Error posting: {e}")
            posted_map[item["id"]] = {"uri": "", "cid": ""}
            save_posted_map(posted_map)
            with open(POSTED_FILE, "a") as f:
                f.write(url + "\n")
            continue

        # Wait with realistic delay before next post
        if i < len(new_items) - 1:
            delay = int(delays[i])
            mins, secs = divmod(delay, 60)
            if mins > 0:
                print(f"  Waiting {mins}m {secs}s (matching tweet gap)...")
            else:
                print(f"  Waiting {secs}s (matching tweet gap)...")
            time.sleep(delay)

    print("\nDone!")


if __name__ == "__main__":
    main()
