#!/usr/bin/env python3
"""Twitter-to-Bluesky mirroring bot using Twitter's GraphQL API.

Config-driven: reads mirrors.json for account pairs.
Each mirror gets its own state directory (state/<name>/).

Supports:
- Quote tweets (inline quoted text)
- Images and videos (downloaded from Twitter, uploaded to Bluesky)
- Realistic delays between posts based on tweet timestamps
- Duplicate prevention via posted_map.json
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from atproto import Client, client_utils, models

BSKY_CHAR_LIMIT = 300
BASE_DIR = Path(__file__).parent

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # 5 minutes

# --- Redis state (Upstash REST API) ---

_REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def _redis_available() -> bool:
    return bool(_REDIS_URL and _REDIS_TOKEN)


def _redis_headers() -> dict:
    return {"Authorization": f"Bearer {_REDIS_TOKEN}"}


def _redis_get_json(key: str):
    try:
        r = httpx.get(f"{_REDIS_URL}/get/{key}", headers=_redis_headers(), timeout=5)
        result = r.json().get("result")
        return json.loads(result) if result else None
    except Exception as e:
        print(f"  Redis GET error {key}: {e}")
        return None


def _redis_set_json(key: str, value) -> None:
    try:
        httpx.post(
            _REDIS_URL,
            headers={**_redis_headers(), "Content-Type": "application/json"},
            json=["SET", key, json.dumps(value)],
            timeout=10,
        )
    except Exception as e:
        print(f"  Redis SET error {key}: {e}")

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
# Max video size for Bluesky (25MB — keep under Render free tier memory)
BSKY_MAX_VIDEO_SIZE = 25_000_000


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
    if _redis_available():
        data = _redis_get_json(f"mirror:{cfg.name}:posted_map")
        if data is not None:
            return data
    # Fallback to local file (local dev / first boot before migration)
    if cfg.posted_map_file.exists():
        try:
            return json.loads(cfg.posted_map_file.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_posted_map(cfg: MirrorConfig, posted_map: dict) -> None:
    if _redis_available():
        _redis_set_json(f"mirror:{cfg.name}:posted_map", posted_map)
    else:
        cfg.posted_map_file.write_text(json.dumps(posted_map, indent=2) + "\n")


def load_posted_urls(cfg: MirrorConfig) -> set[str]:
    if _redis_available():
        return set()  # posted_map is authoritative when Redis is active
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
    if not _redis_available():
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
        skipped_reply = 0
        skipped_retweet = 0
        skipped_age = 0
        try:
            instructions = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
        except (KeyError, TypeError):
            return results
        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                content = entry.get("content", {})
                entry_type = content.get("entryType", "?")

                # Build list of (synthetic) entries to process — modules yield multiple
                if entry_type == "TimelineTimelineModule":
                    candidate_entries = [
                        {"content": {"entryType": "TimelineTimelineItem",
                                     "itemContent": item.get("item", {}).get("itemContent", {})}}
                        for item in content.get("items", [])
                    ]
                elif entry_type == "TimelineTimelineItem":
                    candidate_entries = [entry]
                else:
                    continue  # cursors, who-to-follow modules, etc.

                for candidate in candidate_entries:
                    tweet = self._parse_entry(candidate, user_id, twitter_username)
                    if tweet is None:
                        continue
                    if tweet == "RETWEET":
                        skipped_retweet += 1
                        continue
                    if tweet == "REPLY":
                        skipped_reply += 1
                        continue
                    # Skip tweets older than 30 days (catches pinned/recommended old posts)
                    if tweet.get("created_at"):
                        try:
                            tweet_dt = datetime.fromisoformat(tweet["created_at"])
                            if tweet_dt < cutoff:
                                skipped_age += 1
                                continue
                        except ValueError:
                            pass
                    # Deduplicate (same tweet can appear in both module and standalone entry)
                    if not any(r["id"] == tweet["id"] for r in results):
                        results.append(tweet)
        if skipped_reply or skipped_retweet or skipped_age:
            print(f"    Filtered: {skipped_reply} replies-to-others, "
                  f"{skipped_retweet} retweets, {skipped_age} older-than-30d")
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
        author_user_id = user_results.get("rest_id", "")
        author_screen_name = user_results.get("legacy", {}).get("screen_name", twitter_username)
        tweet_url = f"https://x.com/{author_screen_name}/status/{tweet_id}"

        # Skip tweets by other users (can appear inside conversation modules)
        if author_user_id and author_user_id != user_id:
            return None

        # Skip retweets
        if legacy.get("retweeted_status_result"):
            return "RETWEET"

        # URL entities: maps t.co short URL -> real expanded URL (YouTube, articles, etc.)
        # These are external links that should be preserved in the post text.
        # Media t.co links (not in entities.urls) will be stripped as before.
        url_entities: dict[str, str] = {
            u["url"]: u["expanded_url"]
            for u in legacy.get("entities", {}).get("urls", [])
            if u.get("url") and u.get("expanded_url")
        }

        # Quote tweet
        quoted_text = None
        quoted_user = None
        quoted_url_entities: dict[str, str] = {}
        quoted_media = []
        qt_result = tweet_result.get("quoted_status_result", {}).get("result", {})
        if qt_result:
            if "tweet" in qt_result:
                qt_result = qt_result["tweet"]
            qt_legacy = qt_result.get("legacy", {})
            quoted_text = qt_legacy.get("full_text", "")
            qt_user = qt_result.get("core", {}).get("user_results", {}).get("result", {}).get("legacy", {})
            quoted_user = qt_user.get("screen_name")
            quoted_url_entities = {
                u["url"]: u["expanded_url"]
                for u in qt_legacy.get("entities", {}).get("urls", [])
                if u.get("url") and u.get("expanded_url")
            }
            # Extract media from quoted tweet
            qt_extended = (
                qt_legacy.get("extended_entities", {}).get("media", [])
                or qt_legacy.get("entities", {}).get("media", [])
            )
            for m in qt_extended:
                media_type = m.get("type", "")
                if media_type == "photo":
                    quoted_media.append({
                        "type": "image",
                        "url": m.get("media_url_https", ""),
                        "alt": m.get("ext_alt_text", ""),
                    })
                elif media_type in ("video", "animated_gif"):
                    variants = m.get("video_info", {}).get("variants", [])
                    mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                    if mp4s:
                        best = max(mp4s, key=lambda v: v.get("bitrate", 0))
                        quoted_media.append({
                            "type": "video",
                            "url": best["url"],
                            "content_type": "video/mp4",
                            "duration_ms": m.get("video_info", {}).get("duration_millis", 0),
                            "width": m.get("original_info", {}).get("width", 0),
                            "height": m.get("original_info", {}).get("height", 0),
                        })

        # Timestamp
        created_at = legacy.get("created_at", "")
        tweet_time = None
        if created_at:
            try:
                tweet_time = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
            except ValueError:
                pass

        # Media (images and videos)
        # extended_entities is the primary source; entities.media is a fallback
        media_items = []
        extended = (
            legacy.get("extended_entities", {}).get("media", [])
            or legacy.get("entities", {}).get("media", [])
        )
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

        # Fallback: check Twitter card for video (used for some native uploads)
        # Also checks for HLS streams (.m3u8) used by MLB.com and other embeds
        if not media_items:
            card = tweet_result.get("card", {}).get("legacy", {})
            card_values = {
                b["key"]: b.get("value", {})
                for b in card.get("binding_values", [])
            }
            player_url = (
                card_values.get("player_stream_url", {}).get("string_value")
                or card_values.get("amplify_url_vmap", {}).get("string_value")
            )
            if player_url and (player_url.endswith(".mp4") or player_url.endswith(".m3u8")):
                media_items.append({
                    "type": "video",
                    "url": player_url,
                    "content_type": "video/mp4",
                    "duration_ms": 0,
                    "width": 0,
                    "height": 0,
                })

        # Skip all replies (including self-replies) — only mirror top-level original tweets
        if legacy.get("in_reply_to_user_id_str"):
            return "REPLY"

        return {
            "text": text,
            "url": tweet_url,
            "id": tweet_id,
            "created_at": tweet_time,
            "quoted_text": quoted_text,
            "quoted_user": quoted_user,
            "url_entities": url_entities,
            "quoted_url_entities": quoted_url_entities,
            "media": media_items,
            "quoted_media": quoted_media,
        }


# --- Tweet fetching ---

def fetch_tweets(cfg: MirrorConfig, count: int = 20) -> list[dict] | None:
    cookies = os.environ.get("TWITTER_COOKIES", "")

    # Strategy 1: Cookies (most reliable for recent tweets)
    if cookies:
        tc = TwitterClient()
        if tc.activate_cookies(cookies):
            user_id = tc.get_user_id(cfg.twitter_username)
            if user_id:
                print(f"  Resolved @{cfg.twitter_username} -> ID {user_id}")
                tweets = tc.get_user_tweets(user_id, cfg.twitter_username, count=count)
                if tweets:
                    print(f"  Fetched {len(tweets)} tweets (cookie auth)")
                    return tweets

    # Strategy 2: Guest token
    tc2 = TwitterClient()
    if tc2.activate_guest():
        user_id = tc2.get_user_id(cfg.twitter_username)
        if user_id:
            print(f"  Resolved @{cfg.twitter_username} -> ID {user_id}")
            tweets = tc2.get_user_tweets(user_id, cfg.twitter_username, count=count)
            if tweets:
                print(f"  Fetched {len(tweets)} tweets (guest token)")
                return tweets

    print("  Error: Could not fetch tweets")
    return None


# --- Media handling ---

def download_media(url: str, max_size: int | None = None) -> bytes | None:
    """Download media from Twitter. Returns bytes or None on failure.

    If max_size is given, the download is aborted as soon as accumulated bytes
    exceed the limit — so we never load a 200MB video into memory just to
    discover it's over the 25MB cap.
    """
    try:
        with httpx.stream("GET", url, timeout=60, follow_redirects=True) as resp:
            resp.raise_for_status()
            # Fast path: check Content-Length header before downloading anything
            content_length = resp.headers.get("content-length")
            if max_size and content_length:
                try:
                    if int(content_length) > max_size:
                        mb = int(content_length) / 1_000_000
                        print(f"    Warning: Media too large ({mb:.1f}MB per Content-Length), skipping")
                        return None
                except ValueError:
                    pass
            # Stream in chunks, bail out if we exceed the limit mid-download
            chunks: list[bytes] = []
            downloaded = 0
            for chunk in resp.iter_bytes(chunk_size=65536):
                downloaded += len(chunk)
                if max_size and downloaded > max_size:
                    mb = max_size / 1_000_000
                    print(f"    Warning: Media exceeded {mb:.0f}MB limit during download, skipping")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
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
        # Cap initial download at 10MB; Twitter photos are never legitimately larger
        data = download_media(img["url"], max_size=10_000_000)
        if not data:
            continue
        # Resize URL trick: Twitter serves smaller images with ?name=small
        if len(data) > BSKY_MAX_IMAGE_SIZE:
            data = download_media(img["url"] + "?name=small", max_size=BSKY_MAX_IMAGE_SIZE * 2)
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
    """Upload a video to Bluesky using the dedicated video service and return an embed.

    Bluesky requires videos to be processed through video.bsky.app to generate
    the HLS playlist that the player needs. Raw upload_blob uploads produce
    unplayable 'Video not found' embeds.
    """
    # max_size enforced during streaming — we never load more than 25MB into RAM
    data = download_media(video_item["url"], max_size=BSKY_MAX_VIDEO_SIZE)
    if not data:
        return None

    did = bsky_client.me.did

    # Step 1: Get a service auth token via the atproto SDK.
    # The `aud` must be the user's PDS DID — the video service validates the
    # token against the user's PDS as the trust anchor, NOT did:web:video.bsky.app.
    try:
        pds_endpoint = getattr(bsky_client._session, "pds_endpoint", "https://bsky.social")
        pds_host = urlparse(pds_endpoint).hostname or "bsky.social"
        pds_did = f"did:web:{pds_host}"
        sa = bsky_client.com.atproto.server.get_service_auth(
            params={"aud": pds_did, "lxm": "com.atproto.repo.uploadBlob"}
        )
        service_token = sa.token
        print(f"    Service auth token obtained for {pds_did} ({len(service_token)} chars)")
    except Exception as e:
        print(f"    Warning: Could not get service auth token: {e}")
        return None

    # Step 2: Upload video bytes to the video processing service
    try:
        upload_resp = httpx.post(
            "https://video.bsky.app/xrpc/app.bsky.video.uploadVideo",
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "video/mp4",
            },
            content=data,
            params={"did": did, "name": "video.mp4"},
            timeout=120,
        )
        if upload_resp.status_code != 200:
            print(f"    Warning: Video upload returned HTTP {upload_resp.status_code}")
            print(f"    Response: {upload_resp.text[:300]}")
            return None
        # uploadVideo returns the jobStatus object directly (not wrapped)
        job = upload_resp.json()
        job_id = job.get("jobId")
        blob_dict = job.get("blob")  # present immediately when already processed
        state = job.get("state", "")
        print(f"    Upload accepted: job={job_id}, state={state}")
    except Exception as e:
        print(f"    Warning: Video upload request failed: {e}")
        return None

    # Step 3: Poll getJobStatus until the blob is ready
    # getJobStatus wraps its response in {"jobStatus": {...}}
    if not blob_dict and job_id and state != "JOB_STATE_COMPLETED":
        print(f"    Waiting for video processing...")
        for attempt in range(30):
            time.sleep(2)
            try:
                status_resp = httpx.get(
                    "https://video.bsky.app/xrpc/app.bsky.video.getJobStatus",
                    params={"jobId": job_id},
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=15,
                )
                job_status = status_resp.json().get("jobStatus", {})
                current_state = job_status.get("state", "")
                if job_status.get("blob"):
                    blob_dict = job_status["blob"]
                    print(f"    Video ready after {(attempt + 1) * 2}s (state={current_state})")
                    break
                if current_state == "JOB_STATE_FAILED":
                    print(f"    Warning: Video processing failed: {job_status.get('error')}")
                    return None
                if attempt % 5 == 4:
                    print(f"    Still processing... {(attempt + 1) * 2}s elapsed (state={current_state})")
            except Exception as poll_err:
                print(f"    Warning: Error polling job status: {poll_err}")

    if not blob_dict:
        print(f"    Warning: No blob available after video processing")
        return None

    # Step 4: Build the video embed from the blob dict returned by the service.
    # blob_dict = {"$type": "blob", "ref": {"$link": "bafkrei..."}, "mimeType": "...", "size": N}
    # Use BlobRef.model_validate to let atproto parse aliases/types correctly.
    try:
        from atproto_client.models.blob_ref import BlobRef
        blob_ref = BlobRef.model_validate(blob_dict)
        return models.AppBskyEmbedVideo.Main(video=blob_ref, alt="")
    except Exception as e:
        print(f"    Warning: Could not parse video blob ref: {e}")
        print(f"    Blob dict was: {blob_dict}")
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

def clean_tweet_text(text: str, url_entities: dict[str, str] | None = None) -> str:
    # Twitter's API returns HTML-encoded text (&gt; &lt; &amp; etc.)
    text = html.unescape(text)
    text = re.sub(r"^RT @\w+:\s*", "", text)
    # Expand known t.co links to their real URLs (YouTube, articles, etc.)
    # before stripping — so external links survive in the post text.
    for tco, expanded in (url_entities or {}).items():
        text = text.replace(tco, expanded)
    # Strip remaining t.co links (media attachments appended by Twitter)
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



# --- Run one mirror ---

def run_mirror(cfg: MirrorConfig, fetch_count: int = 20):
    print(f"\n{'='*50}")
    print(f"Mirror: @{cfg.twitter_username} -> {cfg.bsky_handle}")
    print(f"{'='*50}")

    bsky_password = os.environ.get(cfg.bsky_password_env, "")
    if not bsky_password:
        print(f"  Skipping — {cfg.bsky_password_env} not set")
        return

    tweet_items = fetch_tweets(cfg, count=fetch_count)
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
        text = clean_tweet_text(item["text"], item.get("url_entities"))
        url = item["url"]
        quoted_text = item.get("quoted_text")
        if quoted_text:
            quoted_text = clean_tweet_text(quoted_text, item.get("quoted_url_entities"))
        post_text = format_post(text, url, quoted_text, item.get("quoted_user"))

        print(f"\n  [{i + 1}/{len(new_items)}] post: {url}")
        print(f"    Text: {post_text[:80]}...")

        # Build media embed (with failsafe — never crash on media failure)
        # Fall back to quoted tweet's media if original has none
        embed = None
        media = item.get("media", []) or item.get("quoted_media", [])
        if media:
            source = "quoted tweet" if not item.get("media") else "tweet"
            print(f"    Media: {len(media)} item(s) (from {source})")
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

    # --count N: fetch more tweets than the default 20 (useful for backfill runs)
    fetch_count = 20
    for i, arg in enumerate(sys.argv):
        if arg == "--count" and i + 1 < len(sys.argv):
            try:
                fetch_count = int(sys.argv[i + 1])
            except ValueError:
                pass

    # --remove-ids id1 id2 ...: un-seed specific tweet IDs so the bot will post them
    remove_ids: list[str] = []
    for i, arg in enumerate(sys.argv):
        if arg == "--remove-ids":
            j = i + 1
            while j < len(sys.argv) and not sys.argv[j].startswith("--"):
                remove_ids.append(sys.argv[j])
                j += 1

    if remove_ids:
        for cfg in mirrors:
            posted_map = load_posted_map(cfg)
            removed = [id_ for id_ in remove_ids if id_ in posted_map]
            for id_ in removed:
                del posted_map[id_]
            if removed:
                save_posted_map(cfg, posted_map)
                print(f"  [{cfg.name}] Removed {len(removed)} ID(s) from posted state: {removed}")
            else:
                print(f"  [{cfg.name}] None of the specified IDs were in posted state")

    if "--seed" in sys.argv:
        for cfg in mirrors:
            seed(cfg)
        return

    def run_all():
        for cfg in mirrors:
            try:
                run_mirror(cfg, fetch_count=fetch_count)
            except Exception as e:
                print(f"\n  Error running mirror @{cfg.twitter_username}: {e}")
                print("  Continuing to next mirror...")
                continue
        print("\nAll mirrors complete!")

    if "--once" in sys.argv:
        run_all()
        return

    # Continuous polling loop (for Render)
    while True:
        run_all()
        print(f"Sleeping {POLL_INTERVAL}s until next poll...\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
