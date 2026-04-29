#!/usr/bin/env python3
"""Delete a specific Bluesky post by rkey."""
import os, sys
from atproto import Client

handle = "alexfast8bot.bsky.social"
pw = os.environ.get("BSKY_APP_PASSWORD_AFAST", "")
rkey = "3mkng2le6db2s"  # tweet 2049290037884330072 — posted without video

if not pw:
    print("BSKY_APP_PASSWORD_AFAST not set"); sys.exit(1)

client = Client()
client.login(handle, pw)
did = client.me.did
client.app.bsky.feed.post.delete(did, rkey)
print(f"Deleted post {rkey} from {handle}")
