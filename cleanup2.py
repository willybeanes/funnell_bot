#!/usr/bin/env python3
"""Delete the broken alexfast8 DeGrom video post (uploaded without HLS processing)."""
import os, sys, json
from pathlib import Path
from atproto import Client

handle = "alexfast8bot.bsky.social"
pw = os.environ.get("BSKY_APP_PASSWORD_AFAST", "")

# The post that was re-uploaded with the old broken code (upload_blob fallback)
rkey = "3mknir26ywr2c"   # tweet 2049232458508337556 — DeGrom overlay, video not processed
tweet_id = "2049232458508337556"

if not pw:
    print("BSKY_APP_PASSWORD_AFAST not set"); sys.exit(1)

client = Client()
client.login(handle, pw)
did = client.me.did

# Delete the broken post from Bluesky
try:
    client.app.bsky.feed.post.delete(did, rkey)
    print(f"Deleted post {rkey}")
except Exception as e:
    print(f"Could not delete {rkey}: {e}")

# Remove the tweet from the posted state so the mirror will re-post it
state_file = Path(__file__).parent / "state" / "alexfast8" / "posted_map.json"
if state_file.exists():
    data = json.loads(state_file.read_text())
    if tweet_id in data:
        del data[tweet_id]
        state_file.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Removed {tweet_id} from posted state")
    else:
        print(f"{tweet_id} not found in posted state")
