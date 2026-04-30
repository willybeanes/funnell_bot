#!/usr/bin/env python3
"""Seed lancebrozbot: mark all current tweets as posted, then unseed the one to repost."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mirror import load_mirrors, fetch_tweets, load_posted_map, save_posted_map

TARGET_TWEET = "2049889701968884065"  # the tweet to (re)post fresh

cfg = next(m for m in load_mirrors() if m.name == "lancebrozbot")
tweet_items = fetch_tweets(cfg)
if not tweet_items:
    print("Could not fetch tweets"); sys.exit(1)

posted_map = load_posted_map(cfg)

# Seed all fetched tweets
seeded = 0
for item in tweet_items:
    if item["id"] not in posted_map:
        posted_map[item["id"]] = {"uri": "", "cid": ""}
        with open(cfg.posted_file, "a") as f:
            f.write(item["url"] + "\n")
        seeded += 1

save_posted_map(cfg, posted_map)
print(f"Seeded {seeded} tweets ({len(posted_map)} total)")

# Unseed the target so it gets picked up fresh
if TARGET_TWEET in posted_map:
    del posted_map[TARGET_TWEET]
    save_posted_map(cfg, posted_map)
    # Also remove from posted.txt
    tweet_url = f"https://x.com/LanceBroz/status/{TARGET_TWEET}"
    lines = cfg.posted_file.read_text().splitlines()
    cfg.posted_file.write_text("\n".join(l for l in lines if l.strip() != tweet_url) + "\n")
    print(f"Unseeded {TARGET_TWEET} for fresh posting")
else:
    print(f"Note: {TARGET_TWEET} not in fetched timeline, nothing to unseed")
