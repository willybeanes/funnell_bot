#!/usr/bin/env python3
"""Delete two specific alexfast8 posts that were posted without video."""
import os, sys
from atproto import Client

handle = "alexfast8bot.bsky.social"
pw = os.environ.get("BSKY_APP_PASSWORD_AFAST", "")
# Both tweets need reposting with the fixed video upload
rkeys = [
    "3mknfv74lgz2c",   # tweet 2049232458508337556 — DeGrom overlay (video not rendered)
    "3mkngvh5xqz2s",   # tweet 2049290037884330072 — re-posted but still no video detected
]

if not pw:
    print("BSKY_APP_PASSWORD_AFAST not set"); sys.exit(1)

client = Client()
client.login(handle, pw)
did = client.me.did
for rkey in rkeys:
    try:
        client.app.bsky.feed.post.delete(did, rkey)
        print(f"Deleted {rkey}")
    except Exception as e:
        print(f"Could not delete {rkey}: {e}")
