#!/usr/bin/env python3
"""One-time migration: copy local state files → Upstash Redis."""

import json
import os
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

        posted_map_file = mirror_dir / "posted_map.json"
        if posted_map_file.exists():
            posted_map = json.loads(posted_map_file.read_text())
            redis_set(f"mirror:{name}:posted_map", posted_map)
            print(f"  {len(posted_map)} entries in posted_map")
        else:
            print(f"  No posted_map.json found, skipping")

    print("\nMigration complete!")


if __name__ == "__main__":
    main()
