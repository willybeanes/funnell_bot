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

state_dir = Path(__file__).parent / "state" / "dadler"

# Remove from posted_map.json
map_file = state_dir / "posted_map.json"
if map_file.exists():
    data = json.loads(map_file.read_text())
    if tweet_id in data:
        del data[tweet_id]
        map_file.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Removed {tweet_id} from posted_map.json")

# Remove from posted.txt (is_posted checks both files)
txt_file = state_dir / "posted.txt"
tweet_url = f"https://x.com/_dadler/status/{tweet_id}"
if txt_file.exists():
    lines = txt_file.read_text().splitlines()
    filtered = [l for l in lines if l.strip() != tweet_url]
    if len(filtered) < len(lines):
        txt_file.write_text("\n".join(filtered) + "\n")
        print(f"Removed {tweet_url} from posted.txt")
