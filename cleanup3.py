#!/usr/bin/env python3
"""Delete broken dadler video post (posted without HLS video processing) and unseed it."""
import os, sys, json
from pathlib import Path
from atproto import Client

handle = "dadler-bot.bsky.social"
pw = os.environ.get("BSKY_APP_PASSWORD_DADLER", "")

rkey = "3mkq7cbbocc2a"           # tweet 2049911840067428693 — James Wood video, no HLS
tweet_id = "2049911840067428693"

if not pw:
    print("BSKY_APP_PASSWORD_DADLER not set"); sys.exit(1)

client = Client()
client.login(handle, pw)
did = client.me.did

try:
    client.app.bsky.feed.post.delete(did, rkey)
    print(f"Deleted post {rkey}")
except Exception as e:
    print(f"Could not delete {rkey}: {e}")

state_file = Path(__file__).parent / "state" / "dadler" / "posted_map.json"
if state_file.exists():
    data = json.loads(state_file.read_text())
    if tweet_id in data:
        del data[tweet_id]
        state_file.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Removed {tweet_id} from posted state")
    else:
        print(f"{tweet_id} not in posted state")
