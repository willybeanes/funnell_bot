#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twitter's GraphQL API.

Config-driven: reads mirrors.json for account pairs.
Each mirror gets its own state directory (state/<name>/).

Supports:
- Quote tweets (inline quoted text)
- Thread detection (self-replies become Bluesky reply chains)
- Images and videos (downloaded from Twitter, uploaded to Bluesky)
- Realistic delays between posts based on tweet timestamps
- Duplicate prevention via posted_map.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from atproto import Client, client_utils, models

BSKY_CHAR_LIMIT = 300
BASE_DIR = Path(__file__).parent

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

# Max image size for Bluesky (1MB)
BSKY_MAX_IMAGE_SIZE = 1_000_000
# Max video size for Bluesky (50MB)
BSKY_MAX_VIDEO_SIZE = 50_000_000


# --- Mirror config ---

class MirrorConfig:
    def __init__(self, config: dict):
        self.name = config["name"]
        self.twitter_username = config["twitter_username"]
        self.bsky_handle = config["bsky_handle"]
        self.bsky_password_env = config["bsky_password_env"]
        self.state_dir = BASE_DIR / "state" / self.name
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.posted_file = self.state_dir / "posted.txt"
        self.posted_map_file = self.state_dir / "posted_map.json"


def load_mirrors() -> list[MirrorConfig]:
    config_path = BASE_DIR / "mirrors.json"
    configs = json.loads(config_path.read_text())
    return [MirrorConfig(c) for c in configs]


# --- State tracking ---

def load_posted_map(cfg: MirrorConfig) -> dict:
    if cfg.posted_map_file.exists():
        try:
            return json.loads(cfg.posted_map_file.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_posted_map(cfg: MirrorConfig, posted_map: dict) -> None:
    cfg.posted_map_file.write_text(json.dumps(posted_map, indent=2) + "\n")


def load_posted_urls(cfg: MirrorConfig) -> set[str]:
    if not cfg.posted_file.exists():
        return set()
    return set(cfg.posted_file.read_text().strip().splitlines())


def is_posted(tweet_id: str, cfg: MirrorConfig, posted_map: dict, posted_urls: set[str]) -> bool:
    if tweet_id in posted_map:
        return True
    url = f"https://x.com/{cfg.twitter_username}/status/{tweet_id}"
    return url in posted_urls


def record_posted(tweet_id: str, tweet_url: str, bsky_uri: str, bsky_cid: str,
                  cfg: MirrorConfig, posted_map: dict) -> None:
    posted_map[tweet_id] = {"uri": bsky_uri, "cid": bsky_cid}
    save_posted_map(cfg, posted_map)
    with open(cfg.posted_file, "a") as f:
        f.write(tweet_url + "\n")


# --- Twitter GraphQL client ---

class TwitterClient:
    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True)
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

    def activate_guest(self) -> bool:
        try:
            resp = self.client.post(
                "https://api.twitter.com/1.1/guest/activate.json",
                headers=self.headers,
            )
            if resp.status_code == 200:
                token = resp.json().get("guest_token")
                if token:
                    self.headers["x-guest-token"] = token
                    print("  Activated guest token")
                    return True
        except Exception as e:
            print(f"  Error getting guest token: {e}")
        return False

    def activate_cookies(self, cookies_str: str) -> bool:
        try:
            cookie_dict = {}
            for part in cookies_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()
            if "ct0" in cookie_dict:
                self.headers["x-csrf-token"] = cookie_dict["ct0"]
            self.headers.pop("x-guest-token", None)
            self.client = httpx.Client(timeout=30, follow_redirects=True, cookies=cookie_dict)
            print("  Using cookie-based auth")
            return True
        except Exception as e:
            print(f"  Error setting up cookies: {e}")
            return False

    def _graphql_get(self, endpoint: str, variables: dict) -> dict | None:
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(GRAPHQL_FEATURES),
        }
        try:
            resp = self.client.get(
                f"https://twitter.com/i/api/graphql/{endpoint}",
                params=params, headers=self.headers,
            )
            if resp.status_code == 200:
                return resp.json()
            print(f"    GraphQL returned {resp.status_code}")
            return None
        except Exception as e:
            print(f"    GraphQL error: {e}")
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

    def get_user_tweets(self, user_id: str, twitter_username: str, count: int = 20) -> list[dict] | None:
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
        return self._parse_timeline(data, user_id, twitter_username)

    def _parse_timeline(self, data: dict, user_id: str, twitter_username: str) -> list[dict]:
        results = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            instructions = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
        except (KeyError, TypeError):
            return results
        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                tweet = self._parse_entry(entry, user_id, twitter_username)
                if tweet:
                    # Skip tweets older than 30 days (catches pinned/recommended old posts)
                    if tweet.get("created_at"):
                        try:
                            tweet_dt = datetime.fromisoformat(tweet["created_at"])
                            if tweet_dt < cutoff:
                                continue
                        except ValueError:
                            pass
                    results.append(tweet)
        # Hard cap: Twitter may return far more than requested count
        return results[:20]

    def _parse_entry(self, entry: dict, user_id: str, twitter_username: str) -> dict | None:
        content = entry.get("content", {})
        if content.get("entryType") != "TimelineTimelineItem":
            return None

        tweet_result = (
            content.get("itemContent", {})
            .get("tweet_results", {})
            .get("result", {})
        )
        if "tweet" in tweet_result:
            tweet_result = tweet_result["tweet"]

        legacy = tweet_result.get("legacy", {})
        tweet_id = legacy.get("id_str")
        text = legacy.get("full_text", "")
        if not tweet_id or not text:
            return None

        core = tweet_result.get("core", {})
        user_results = core.get("user_results", {}).get("result", {})
        author_screen_name = user_results.get("legacy", {}).get("screen_name", twitter_username)
        tweet_url = f"https://x.com/{author_screen_name}/status/{tweet_id}"

        # Skip retweets
        if legacy.get("retweeted_status_result"):
            return None

        # Quote tweet
        quoted_text = None
        quoted_user = None
        qt_result = tweet_result.get("quoted_status_result", {}).get("result", {})
        if qt_result:
            qt_legacy = qt_result.get("legacy", {})
            quoted_text = qt_legacy.get("full_text", "")
            qt_user = qt_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
            quoted_user = qt_user.get("screen_name")

        # Timestamp
        created_at = legacy.get("created_at", "")
        tweet_time = None
        if created_at:
            try:
                tweet_time = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
            except ValueError:
                pass

        # Media (images and videos)
        media_items = []
        extended = legacy.get("extended_entities", {}).get("media", [])
        for m in extended:
            media_type = m.get("type", "")
            if media_type == "photo":
                media_items.append({
                    "type": "image",
                    "url": m.get("media_url_https", ""),
                    "alt": m.get("ext_alt_text", ""),
                })
            elif media_type in ("video", "animated_gif"):
                # Pick highest bitrate mp4 variant
                variants = m.get("video_info", {}).get("variants", [])
                mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                if mp4s:
                    best = max(mp4s, key=lambda v: v.get("bitrate", 0))
                    media_items.append({
                        "type": "video",
                        "url": best["url"],
                        "content_type": "video/mp4",
                        "duration_ms": m.get("video_info", {}).get("duration_millis", 0),
                        "width": m.get("original_info", {}).get("width", 0),
                        "height": m.get("original_info", {}).get("height", 0),
                    })

        # Thread detection
        reply_to_tweet_id = None
        reply_to_user_id = legacy.get("in_reply_to_user_id_str")
        if reply_to_user_id:
            if reply_to_user_id == user_id:
                reply_to_tweet_id = legacy.get("in_reply_to_status_id_str")
            else:
                return None

        return {
            "text": text,
            "url": tweet_url,
            "id": tweet_id,
            "created_at": tweet_time,
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
            "reply_to_tweet_id": reply_to_tweet_id,
            "media": media_items,
        }


# --- Tweet fetching ---

def fetch_tweets(cfg: MirrorConfig) -> list[dict] | None:
    cookies = os.environ.get("TWITTER_COOKIES", "")

    # Strategy 1: Cookies (most reliable for recent tweets)
    if cookies:
        tc = TwitterClient()
        if tc.activate_cookies(cookies):
            user_id = tc.get_user_id(cfg.twitter_username)
            if user_id:
                print(f"  Resolved @{cfg.twitter_username} -> ID {user_id}")
                tweets = tc.get_user_tweets(user_id, cfg.twitter_username, count=20)
                if tweets:
                    print(f"  Fetched {len(tweets)} tweets (cookie auth)")
                    return tweets

    # Strategy 2: Guest token
    tc2 = TwitterClient()
    if tc2.activate_guest():
        user_id = tc2.get_user_id(cfg.twitter_username)
        if user_id:
            print(f"  Resolved @{cfg.twitter_username} -> ID {user_id}")
            tweets = tc2.get_user_tweets(user_id, cfg.twitter_username, count=20)
            if tweets:
                print(f"  Fetched {len(tweets)} tweets (guest token)")
                return tweets

    print("  Error: Could not fetch tweets")
    return None


# --- Media handling ---

def download_media(url: str) -> bytes | None:
    """Download media from Twitter. Returns bytes or None on failure."""
    try:
        resp = httpx.get(url, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"    Warning: Failed to download media: {e}")
        return None


def upload_images_to_bsky(bsky_client: Client, media_items: list[dict]) -> models.AppBskyEmbedImages.Main | None:
    """Upload images to Bluesky and return an embed. Max 4 images."""
    images = [m for m in media_items if m["type"] == "image"][:4]
    if not images:
        return None

    bsky_images = []
    for img in images:
        data = download_media(img["url"])
        if not data:
            continue
        # Resize URL trick: Twitter serves smaller images with ?name=small
        if len(data) > BSKY_MAX_IMAGE_SIZE:
            data = download_media(img["url"] + "?name=small")
            if not data or len(data) > BSKY_MAX_IMAGE_SIZE:
                print(f"    Warning: Image too large, skipping")
                continue
        try:
            blob = bsky_client.upload_blob(data)
            bsky_images.append(models.AppBskyEmbedImages.Image(
                image=blob.blob,
                alt=img.get("alt", ""),
            ))
        except Exception as e:
            print(f"    Warning: Failed to upload image: {e}")
            continue

    if not bsky_images:
        return None
    return models.AppBskyEmbedImages.Main(images=bsky_images)


def upload_video_to_bsky(bsky_client: Client, video_item: dict) -> models.AppBskyEmbedVideo.Main | None:
    """Upload a video to Bluesky and return an embed."""
    data = download_media(video_item["url"])
    if not data:
        return None
    if len(data) > BSKY_MAX_VIDEO_SIZE:
        print(f"    Warning: Video too large ({len(data) / 1_000_000:.1f}MB), skipping")
        return None
    try:
        blob = bsky_client.upload_blob(data)
        return models.AppBskyEmbedVideo.Main(
            video=blob.blob,
            alt="",
        )
    except Exception as e:
        print(f"    Warning: Failed to upload video: {e}")
        return None


def build_media_embed(bsky_client: Client, media_items: list[dict]):
    """Build a Bluesky embed from tweet media. Returns embed or None."""
    if not media_items:
        return None

    videos = [m for m in media_items if m["type"] == "video"]
    images = [m for m in media_items if m["type"] == "image"]

    # Prefer video if present (Bluesky only supports one video per post)
    if videos:
        embed = upload_video_to_bsky(bsky_client, videos[0])
        if embed:
            return embed

    # Fall back to images
    if images:
        embed = upload_images_to_bsky(bsky_client, images)
        if embed:
            return embed

    return None


# --- Bluesky posting ---

def clean_tweet_text(text: str) -> str:
    text = re.sub(r"^RT @\w+:\s*", "", text)
    # Remove t.co links (Twitter appends these for media/quote tweets)
    text = re.sub(r"\s*https://t\.co/\w+", "", text)
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


# --- Run one mirror ---

def run_mirror(cfg: MirrorConfig):
    print(f"\n{'='*50}")
    print(f"Mirror: @{cfg.twitter_username} -> {cfg.bsky_handle}")
    print(f"{'='*50}")

    bsky_password = os.environ.get(cfg.bsky_password_env, "")
    if not bsky_password:
        print(f"  Skipping — {cfg.bsky_password_env} not set")
        return

    tweet_items = fetch_tweets(cfg)
    if tweet_items is None:
        print("  Could not fetch tweets, skipping this mirror")
        return

    posted_map = load_posted_map(cfg)
    posted_urls = load_posted_urls(cfg)

    new_items = []
    for item in reversed(tweet_items):
        if not is_posted(item["id"], cfg, posted_map, posted_urls):
            new_items.append(item)

    if not new_items:
        print("  No new tweets to post")
        return

    # Safety limit: cap new posts to prevent accidental flooding
    MAX_NEW_POSTS = 10
    if len(new_items) > MAX_NEW_POSTS:
        print(f"  WARNING: Found {len(new_items)} new tweets — capping to {MAX_NEW_POSTS} most recent to prevent flooding")
        new_items = new_items[-MAX_NEW_POSTS:]

    print(f"  Found {len(new_items)} new tweet(s) to post")

    bsky_client = Client()
    try:
        bsky_client.login(cfg.bsky_handle, bsky_password)
        print(f"  Logged in to Bluesky as {cfg.bsky_handle}")
    except Exception as e:
        print(f"  Error logging in to Bluesky: {e}")
        return

    # Calculate realistic delays
    MAX_TOTAL_DELAY = 3 * 60  # 3 min per mirror (four mirrors = 12 min max)
    MIN_DELAY = 60  # Never post faster than 1 per minute

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

    total_delay = sum(delays)
    if total_delay > MAX_TOTAL_DELAY and delays:
        scale = MAX_TOTAL_DELAY / total_delay
        delays = [d * scale for d in delays]
        print(f"  Scaled delays to fit in {MAX_TOTAL_DELAY // 60}m")

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

        label = "thread reply" if reply_ref else "post"
        print(f"\n  [{i + 1}/{len(new_items)}] {label}: {url}")
        print(f"    Text: {post_text[:80]}...")

        # Build media embed (with failsafe — never crash on media failure)
        embed = None
        media = item.get("media", [])
        if media:
            print(f"    Media: {len(media)} item(s)")
            try:
                embed = build_media_embed(bsky_client, media)
                if embed:
                    print(f"    Media uploaded successfully")
                else:
                    print(f"    Warning: Media upload failed, posting without media")
            except Exception as e:
                print(f"    Warning: Media error ({e}), posting without media")

        try:
            rich_text = create_rich_post(bsky_client, post_text, url)
            if reply_ref:
                response = bsky_client.send_post(rich_text, reply_to=reply_ref, embed=embed)
            else:
                response = bsky_client.send_post(rich_text, embed=embed)
            record_posted(item["id"], url, response.uri, response.cid, cfg, posted_map)
            print(f"    Posted successfully")
        except Exception as e:
            print(f"    Error posting: {e}")
            posted_map[item["id"]] = {"uri": "", "cid": ""}
            save_posted_map(cfg, posted_map)
            with open(cfg.posted_file, "a") as f:
                f.write(url + "\n")
            continue

        if i < len(new_items) - 1:
            delay = int(delays[i])
            mins, secs = divmod(delay, 60)
            if mins > 0:
                print(f"    Waiting {mins}m {secs}s (matching tweet gap)...")
            else:
                print(f"    Waiting {secs}s...")
            time.sleep(delay)

    print(f"\n  Done with @{cfg.twitter_username}!")


# --- Commands ---

def seed(cfg: MirrorConfig):
    print(f"Seeding @{cfg.twitter_username}...")
    tweet_items = fetch_tweets(cfg)
    if tweet_items is None:
        print("  Could not fetch tweets for seeding")
        return

    posted_map = load_posted_map(cfg)
    posted_urls = load_posted_urls(cfg)
    count = 0
    for item in tweet_items:
        if not is_posted(item["id"], cfg, posted_map, posted_urls):
            posted_map[item["id"]] = {"uri": "", "cid": ""}
            with open(cfg.posted_file, "a") as f:
                f.write(item["url"] + "\n")
            count += 1

    save_posted_map(cfg, posted_map)
    print(f"  Seeded {count} existing tweets (total tracked: {len(posted_map)})")


def main():
    mirrors = load_mirrors()

    # Filter to a specific mirror if --mirror flag is provided
    mirror_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--mirror" and i + 1 < len(sys.argv):
            mirror_filter = sys.argv[i + 1]

    if mirror_filter:
        mirrors = [m for m in mirrors if m.name == mirror_filter]
        if not mirrors:
            print(f"Error: Mirror '{mirror_filter}' not found in mirrors.json")
            sys.exit(1)

    if "--seed" in sys.argv:
        for cfg in mirrors:
            seed(cfg)
        return

    for cfg in mirrors:
        try:
            run_mirror(cfg)
        except Exception as e:
            print(f"\n  Error running mirror @{cfg.twitter_username}: {e}")
            print("  Continuing to next mirror...")
            continue

    print("\nAll mirrors complete!")


if __name__ == "__main__":
    main()
