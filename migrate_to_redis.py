#!/usr/bin/env python3
"""One-time migration: copy local state files → Upstash Redis."""

import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
STATE_DIR = Path(__file__).parent / "state"


def redis_set(key: str, value) -> None:
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}", "Content-Type": "application/json"}
    resp = httpx.post(REDIS_URL, headers=headers, json=["SET", key, json.dumps(value)], timeout=15)
    resp.raise_for_status()
    print(f"  ✓ SET {key}")


def main():
    if not REDIS_URL or not REDIS_TOKEN:
        print("ERROR: UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set in .env")
        return

    for mirror_dir in sorted(STATE_DIR.iterdir()):
        if not mirror_dir.is_dir():
            continue
        name = mirror_dir.name
        print(f"\nMigrating: {name}")

        # Load posted_map (has URI/CID for thread detection)
        posted_map = {}
        posted_map_file = mirror_dir / "posted_map.json"
        if posted_map_file.exists():
            posted_map = json.loads(posted_map_file.read_text())
            print(f"  {len(posted_map)} entries from posted_map.json")

        # Also load posted.txt and extract tweet IDs (catches tweets not in posted_map)
        posted_txt = mirror_dir / "posted.txt"
        if posted_txt.exists():
            urls = posted_txt.read_text().strip().splitlines()
            added = 0
            for url in urls:
                m = re.search(r"/status/(\d+)", url)
                if m:
                    tweet_id = m.group(1)
                    if tweet_id not in posted_map:
                        posted_map[tweet_id] = {"uri": "", "cid": ""}
                        added += 1
            print(f"  {added} additional entries from posted.txt")

        redis_set(f"mirror:{name}:posted_map", posted_map)
        print(f"  {len(posted_map)} total entries in Redis")

    print("\nMigration complete!")


if __name__ == "__main__":
    main()
