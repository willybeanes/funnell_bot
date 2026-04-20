#!/usr/bin/env python3
"""One-time cleanup: delete all posts from a Bluesky account."""

import os
import sys
import time

from atproto import Client, models

def delete_all_posts(handle: str, password: str):
    client = Client()
    client.login(handle, password)
    print(f"Logged in as {handle}")

    did = client.me.did
    deleted = 0
    cursor = None

    while True:
        resp = client.app.bsky.feed.get_author_feed(
            {"actor": did, "limit": 100, "cursor": cursor}
        )
        if not resp.feed:
            break

        for item in resp.feed:
            post = item.post
            # Only delete posts authored by this account
            if post.author.did == did:
                uri = post.uri
                rkey = uri.split("/")[-1]
                try:
                    client.app.bsky.feed.post.delete(did, rkey)
                    deleted += 1
                    if deleted % 10 == 0:
                        print(f"  Deleted {deleted} posts...")
                        time.sleep(0.5)  # Rate limit
                except Exception as e:
                    print(f"  Error deleting {rkey}: {e}")

        cursor = resp.cursor
        if not cursor:
            break
        time.sleep(0.5)

    print(f"Done! Deleted {deleted} total posts from {handle}")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else ""

    if target == "dadler":
        pw = os.environ.get("BSKY_APP_PASSWORD_DADLER", "")
        if not pw:
            print("BSKY_APP_PASSWORD_DADLER not set")
            sys.exit(1)
        delete_all_posts("dadler-bot.bsky.social", pw)
    else:
        print(f"Usage: python cleanup.py dadler")
        sys.exit(1)


if __name__ == "__main__":
    main()
