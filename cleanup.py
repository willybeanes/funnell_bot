#!/usr/bin/env python3
"""Delete specific old posts from Bluesky accounts based on state files.

Finds any posted_map.json entries where the tweet ID doesn't start with '2'
(i.e., old tweets pre-2023) and deletes them from Bluesky, then removes
them from the state files.
"""

import json
import os
import time
from pathlib import Path

from atproto import Client

BASE_DIR = Path(__file__).parent

MIRRORS = [
    {
        "name": "dadler",
        "bsky_handle": "dadler-bot.bsky.social",
        "bsky_password_env": "BSKY_APP_PASSWORD_DADLER",
    },
    {
        "name": "toomuchtuma",
        "bsky_handle": "toomuchtuma-bot.bsky.social",
        "bsky_password_env": "BSKY_APP_PASSWORD_TUMA",
    },
]


def delete_old_posts(mirror: dict):
    name = mirror["name"]
    handle = mirror["bsky_handle"]
    pw = os.environ.get(mirror["bsky_password_env"], "")
    if not pw:
        print(f"  Skipping {name} — {mirror['bsky_password_env']} not set")
        return

    state_dir = BASE_DIR / "state" / name
    map_file = state_dir / "posted_map.json"
    txt_file = state_dir / "posted.txt"

    posted_map = json.loads(map_file.read_text())

    # Find old tweet IDs (not starting with '2' = pre-2023 snowflake range)
    old_ids = {
        tid: v for tid, v in posted_map.items()
        if not tid.startswith("2") and v.get("uri")
    }

    if not old_ids:
        print(f"  {name}: no old posts to delete")
        return

    print(f"  {name}: deleting {len(old_ids)} old posts from Bluesky...")

    client = Client()
    client.login(handle, pw)
    did = client.me.did

    deleted_ids = set()
    for tweet_id, entry in old_ids.items():
        uri = entry["uri"]
        rkey = uri.split("/")[-1]
        try:
            client.app.bsky.feed.post.delete(did, rkey)
            deleted_ids.add(tweet_id)
            print(f"    Deleted post for tweet {tweet_id}")
            time.sleep(0.5)
        except Exception as e:
            print(f"    Error deleting {rkey}: {e}")

    # Remove deleted IDs from state
    for tid in deleted_ids:
        del posted_map[tid]
    map_file.write_text(json.dumps(posted_map, indent=2) + "\n")

    # Remove from posted.txt
    lines = txt_file.read_text().splitlines()
    kept = [l for l in lines if not any(tid in l for tid in deleted_ids)]
    txt_file.write_text("\n".join(kept) + "\n")

    print(f"  {name}: done, removed {len(deleted_ids)} entries from state")


def main():
    print("Cleaning up old posts from all mirrors...")
    for mirror in MIRRORS:
        print(f"\n[{mirror['name']}]")
        delete_old_posts(mirror)
    print("\nDone!")


if __name__ == "__main__":
    main()
