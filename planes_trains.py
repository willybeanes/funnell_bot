#!/usr/bin/env python3
"""Planes, Trains and Automobiles real-time bot.

Posts the events of the 1987 film to planes-trains-auto.bsky.social at the
(approximate) times they happen in the movie, starting the Tuesday before
Thanksgiving and ending with Thanksgiving dinner on Thursday.

Runs as a background thread inside mirror.py (so no extra Render service is
needed), or standalone:

    python planes_trains.py              # run the scheduler loop
    python planes_trains.py --dry-run    # print the schedule, post nothing

Already-posted events are remembered in Upstash Redis (if configured) or in
state/planes_trains/posted.json, so a restart never double-posts. Events whose
time passed more than GRACE_MINUTES ago are skipped instead of posted late.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

BSKY_HANDLE = "planes-trains-auto.bsky.social"
BSKY_PASSWORD_ENV = "BSKY_APP_PASSWORD_PLAINS"
STATE_KEY = "movie:planes_trains:posted"
STATE_FILE = Path(__file__).parent / "state" / "planes_trains" / "posted.json"
CHECK_INTERVAL = 30  # seconds between schedule checks
GRACE_MINUTES = int(os.environ.get("PTA_GRACE_MINUTES", "120"))
BSKY_CHAR_LIMIT = 300

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# Thanksgiving 2026 is Thursday, Nov 26. The film runs Tuesday -> Thursday.
TUE, WED, THU = (2026, 11, 24), (2026, 11, 25), (2026, 11, 26)


def at(day, hh, mm, tz=CT):
    return datetime(*day, hh, mm, tzinfo=tz)


# (id, when, text). Times are local to where the scene takes place and are
# best estimates of when each moment happens in the film.
EVENTS = [
    ("intro", at(TUE, 16, 15, ET),
     "It's Tuesday, two days before Thanksgiving. Starting now, this account will post "
     "the events of Planes, Trains and Automobiles (1987) as they happen, in real time. "
     "Neal Page just wants to get home to Chicago. It's going to be a long trip."),
    ("meeting", at(TUE, 16, 30, ET),
     "Manhattan. Ad exec Neal Page sits in a conference room while his client stares at "
     "ad layouts in total silence. Neal keeps glancing at his watch. He has a 6:00 flight "
     "home to Chicago."),
    ("meeting-ends", at(TUE, 17, 5, ET),
     "The meeting finally breaks up. Neal bolts for the street, where it seems every "
     "person in New York City is trying to hail a cab at the same time."),
    ("cab", at(TUE, 17, 15, ET),
     'Rush hour, snow on the way. Neal bribes a stranger to give up the cab he just hailed, only to watch another man swoop in and take it. Somewhere in this mess is a big, cheerful man with an enormous trunk. Remember that guy.'),
    ("laguardia", at(TUE, 17, 55, ET),
     "Neal makes it to LaGuardia. The flight is delayed. In the waiting area he comes face "
     "to face with the cab thief: Del Griffith, shower curtain ring salesman. "
     "Del has no idea. He'd love to chat."),
    ("six-oclock", at(TUE, 18, 0, ET),
     "6:00 PM. Neal's flight to Chicago was supposed to leave right now. "
     "It has not left."),
    ("coach", at(TUE, 19, 20, ET),
     "Boarding. Neal's first-class ticket has been bumped to coach. His new seatmate? "
     "Del Griffith. Del is thrilled."),
    ("wheels-up", at(TUE, 19, 45, ET),
     "Wheels up out of LaGuardia. Del has taken off his shoes. And his socks. "
     "Neal stares out the window and tries very hard to be somewhere else."),
    ("diverted", at(TUE, 20, 40, ET),
     "Bad news from the cockpit: a blizzard has closed Chicago. The flight is being "
     "diverted to Wichita, Kansas."),
    ("wichita", at(TUE, 20, 10, CT),
     'Touchdown in Wichita. Neal and Del are stranded for the night, and rooms in town are hard to come by.'),
    ("braidwood", at(TUE, 21, 0, CT),
     'The Braidwood Inn. Only one room left. One room. One bed. In the shuffle at check-in, Neal and Del accidentally swap credit cards. Neal is now sharing a bed with the man who stole his cab.'),
    ("have-a-point", at(TUE, 22, 0, CT),
     'Neal finally snaps and unloads on Del: the stories, the noises, the shower curtain rings. "Have a point!" Del takes it quietly, then answers: he likes himself, and his wife likes him. Neal is humbled.'),
    ("lights-out", at(TUE, 22, 45, CT),
     "Lights out in Wichita. Two grown men, one bed, one very small motel room. "
     "Nobody is getting much sleep."),
    ("robbery", at(WED, 2, 30, CT),
     "Middle of the night at the Braidwood Inn. While Neal and Del sleep, a thief slips "
     "into the room and quietly cleans the cash out of their wallets."),
    ("pillows", at(WED, 6, 30, CT),
     "Wednesday morning. Neal and Del wake up a bit closer than they went to sleep. "
     "\"Those aren't pillows!\""),
    ("robbed", at(WED, 7, 15, CT),
     "They discover the robbery. Neal's cash is gone, and so is Del's. "
     "Thanksgiving is tomorrow and they are still in Kansas."),
    ("train", at(WED, 9, 0, CT),
     'Air travel is a mess, so Neal buys train tickets to Chicago for himself and Del. Separate compartments. He bids Del farewell. Planes are over. Trains have begun.'),
    ("breakdown", at(WED, 11, 30, CT),
     'The train breaks down near Jefferson City, Missouri. Passengers are left stranded in a field. Neal spots Del struggling with his giant trunk and, against his better judgment, goes over to help.'),
    ("bus", at(WED, 13, 0, CT),
     "On a bus to St. Louis. Neal tries to lead a sing-along. It does not go well. "
     "Del takes over with the Flintstones theme and the whole bus joins in."),
    ("earrings", at(WED, 14, 30, CT),
     "Low on cash, Del starts selling his shower curtain rings to strangers as earrings. They're selling."),
    ("split", at(WED, 15, 30, CT),
     'Over a meal in St. Louis, Neal suggests they finish the trip separately. Del is hurt. They part ways, and Neal heads to Lambert Airport to rent a car. What could go wrong?'),
    ("rental-lot", at(WED, 16, 15, CT),
     "Neal rides the shuttle way out to the rental lot. His car isn't there. Nothing is there. He trudges all the way back across the airport through the snow."),
    ("rental-rant", at(WED, 17, 0, CT),
     "Back at the car rental counter, Neal unloads an extraordinarily profane rant on the "
     "cheerful agent. She lets him finish, then asks for his rental agreement. "
     "He doesn't have it."),
    ("dispatcher", at(WED, 17, 30, CT),
     'Neal tries to book a taxi to Chicago, insults the dispatcher, and gets punched in the face. Who happens to pull up in a rental car and offer him a ride? Del Griffith.'),
    ("wrong-way", at(WED, 19, 15, CT),
     "Night on the interstate. Del is wrestling with his coat at the wheel. A couple across "
     "the median yells that they're going the wrong way. \"How would he know where we're "
     "going?\""),
    ("trucks", at(WED, 19, 20, CT),
     'They are, in fact, going the wrong way, squeezed between two oncoming semis. Neal looks over and sees the devil at the wheel. Somehow they survive and pull over.'),
    ("fire", at(WED, 20, 15, CT),
     "Del's carelessly tossed cigarette sets the rental car on fire. Neal can't stop laughing. It's Del's rental car, so it's Del's problem. Right?"),
    ("card-twist", at(WED, 20, 25, CT),
     "Then Del confesses: he found Neal's credit card in his wallet, used it to rent the car, "
     "and put it back in Neal's wallet. Which is in the car. Which is on fire. Neal stops laughing."),
    ("motel2", at(WED, 21, 30, CT),
     'His credit cards gone, Neal trades his expensive watch for a motel room and refuses to help Del. With no money, Del tries to sleep outside in the snow and freezing cold.'),
    ("come-in", at(WED, 22, 15, CT),
     'Del wonders aloud how he always drives away the people he likes. Neal hears him and invites him in. Out come the little bottles from the minibar, and the two of them laugh about the whole disaster.'),
    ("thanksgiving", at(THU, 7, 30, CT),
     "Happy Thanksgiving. Neal and Del hit the road in what's left of the burnt rental car. Chicago is still a long way off."),
    ("trooper", at(THU, 9, 30, CT),
     "Pulled over by a state trooper, who impounds the car as unroadworthy. Automobiles: done. They're stranded on the side of the road on Thanksgiving morning."),
    ("truck", at(THU, 10, 30, CT),
     'Del talks a trucker into giving them a ride to Chicago. The catch: they ride in the back, in the refrigerated trailer.'),
    ("chicago", at(THU, 15, 0, CT),
     'Chicago! At the L station, Neal thanks Del for getting him home. They part as friends and Neal boards the train for the last leg.'),
    ("realization", at(THU, 15, 30, CT),
     "On the L, Neal thinks back over Del's odd comments and long silences over the last three days. Something isn't right. He goes back to the station."),
    ("marie", at(THU, 15, 50, CT),
     "Del is still there, sitting alone with his trunk. He admits he has no home. He's been living on the road since his wife Marie died eight years ago."),
    ("home", at(THU, 16, 45, CT),
     'Neal walks through his front door with Del Griffith and his trunk and introduces his family to his new friend. Del is staying for Thanksgiving.'),
    ("end", at(THU, 17, 0, CT),
     "Neal made it home for Thanksgiving. Planes, trains, automobiles, a bus, a truck, and "
     "a lot of shower curtain rings. Happy Thanksgiving from Neal, Del, and everyone at "
     "this account."),
    ("bts-bacon", at(TUE, 17, 40, CT),
     "🎬 Behind the scenes: The man who swoops in and steals Neal's cab is Kevin Bacon. John Hughes was finishing his next film, She's Having a Baby, starring Bacon, while shooting this one, and Bacon agreed to a quick cameo."),
    ("bts-origin", at(TUE, 19, 15, CT),
     '🎬 Behind the scenes: Before Hollywood, John Hughes was an ad copywriter in Chicago, just like Neal. A winter storm wrecked his own flight home from a New York presentation, and his long, roundabout trip back became the seed of this movie.'),
    ("bts-rewrites", at(TUE, 23, 30, CT),
     '🎬 Behind the scenes: Editor Paul Hirsch has said Hughes would rewrite scenes the night before shooting and hand the new pages to Martin and Candy, and the scenes kept getting longer and longer.'),
    ("bts-release", at(WED, 8, 15, CT),
     '🎬 Behind the scenes: Planes, Trains and Automobiles opened in theaters on November 25, 1987, the day before Thanksgiving. 39 years ago today.'),
    ("bts-assembly", at(WED, 10, 15, CT),
     "🎬 Behind the scenes: The finished film runs 93 minutes. According to editor Paul Hirsch, the first assembly ran about 225 minutes. That's nearly four hours of Neal and Del."),
    ("bts-deleted", at(WED, 12, 15, CT),
     '🎬 Behind the scenes: For the 35th anniversary in 2022, roughly 75 minutes of long-lost deleted and extended scenes were released on 4K, much of it John Candy riffing. Some scenes from the original cut are still missing.'),
    ("bts-mcclurg", at(WED, 16, 40, CT),
     '🎬 Behind the scenes: Coming up: the rental counter. Edie McClurg, who plays the cheerful agent, reportedly improvised the Thanksgiving phone call Neal overhears as he walks up to her desk.'),
    ("bts-rating", at(WED, 17, 10, CT),
     "🎬 Behind the scenes: That rant contains the F-word 18 times (19 if you count the agent's reply). It's the only reason the film is rated R, the first R-rated movie John Hughes directed."),
    ("bts-reunion", at(WED, 23, 0, CT),
     '🎬 Behind the scenes: Hughes and John Candy kept working together after this film: Hughes wrote The Great Outdoors (1988) and wrote and directed Uncle Buck (1989), both starring Candy.'),
    ("bts-mckean", at(THU, 11, 30, CT),
     '🎬 Behind the scenes: The state trooper who impounds the car is Michael McKean, best known at the time for co-writing and starring in This Is Spinal Tap.'),
    ("bts-appeal", at(THU, 13, 0, CT),
     '🎬 Behind the scenes: Paramount appealed the R rating, but the MPAA upheld it. The filmmakers also shot a version of the rental car rant without the swearing, for TV broadcasts.'),
    ("bts-candy", at(THU, 17, 30, CT),
     '🎬 Behind the scenes: John Candy died in 1994 at just 43. Del Griffith is remembered as one of his finest roles. Thanks for following along with Neal and Del this week.'),
]


# --- State ------------------------------------------------------------------

_REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def load_done() -> set[str]:
    if _REDIS_URL and _REDIS_TOKEN:
        try:
            r = httpx.get(f"{_REDIS_URL}/get/{STATE_KEY}",
                          headers={"Authorization": f"Bearer {_REDIS_TOKEN}"}, timeout=5)
            result = r.json().get("result")
            return set(json.loads(result)) if result else set()
        except Exception as e:
            print(f"[planes_trains] Redis GET error: {e}")
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_done(done: set[str]) -> None:
    value = sorted(done)
    if _REDIS_URL and _REDIS_TOKEN:
        try:
            httpx.post(_REDIS_URL,
                       headers={"Authorization": f"Bearer {_REDIS_TOKEN}",
                                "Content-Type": "application/json"},
                       json=["SET", STATE_KEY, json.dumps(value)], timeout=10)
        except Exception as e:
            print(f"[planes_trains] Redis SET error: {e}")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(value))


# --- Posting ----------------------------------------------------------------

def login():
    from atproto import Client
    password = os.environ.get(BSKY_PASSWORD_ENV)
    if not password:
        print(f"[planes_trains] {BSKY_PASSWORD_ENV} not set; movie bot disabled.")
        return None
    client = Client()
    client.login(BSKY_HANDLE, password)
    print(f"[planes_trains] Logged in as {BSKY_HANDLE}")
    return client


def run(dry_run: bool = False) -> None:
    events = sorted(EVENTS, key=lambda e: e[1])
    last = events[-1][1]

    if dry_run:
        for eid, when, text in events:
            print(f"{when.astimezone(ET):%a %I:%M %p ET}  [{eid}] ({len(text)} chars)\n  {text}\n")
        return

    client = login()
    if client is None:
        return

    done = load_done()
    # Belt and braces: also skip anything already on the account's feed,
    # so a redeploy without saved state never double-posts.
    try:
        feed = client.get_author_feed(actor=BSKY_HANDLE, limit=100).feed
        live = {item.post.record.text[:80] for item in feed}
        for eid, _, text in events:
            if text[:80] in live:
                done.add(eid)
    except Exception as e:
        print(f"[planes_trains] Could not read own feed: {e}")
    print(f"[planes_trains] {len(done)}/{len(events)} events already handled.")

    while True:
        now = datetime.now(timezone.utc)
        if now > last + timedelta(minutes=GRACE_MINUTES) or len(done) >= len(events):
            print("[planes_trains] The movie is over. Happy Thanksgiving! Stopping.")
            return

        due = next(((eid, when, text) for eid, when, text in events
                    if eid not in done and when <= now), None)
        if due is None:
            time.sleep(CHECK_INTERVAL)
            continue

        eid, when, text = due
        if now - when > timedelta(minutes=GRACE_MINUTES):
            print(f"[planes_trains] Skipping stale event [{eid}] (was due {when}).")
        else:
            try:
                client.send_post(text=text)
                print(f"[planes_trains] Posted [{eid}]: {text[:60]}...")
            except Exception as e:
                print(f"[planes_trains] Post failed for [{eid}]: {e}; retrying shortly.")
                time.sleep(CHECK_INTERVAL)
                try:
                    client = login() or client
                except Exception as e2:
                    print(f"[planes_trains] Re-login failed: {e2}")
                continue
        done.add(eid)
        save_done(done)
        time.sleep(5)  # small gap if catching up on several events


def run_forever() -> None:
    """Entry point for the background thread in mirror.py: never crash the mirror."""
    try:
        run()
    except Exception as e:
        print(f"[planes_trains] Movie bot stopped with error: {e}")


for _eid, _when, _text in EVENTS:
    assert len(_text) <= BSKY_CHAR_LIMIT, f"{_eid} is {len(_text)} chars"


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
