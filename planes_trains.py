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
     "Neal pays a stranger for dibs on a cab, only to watch it get taken out from under him. "
     "Then he loses another one to a big, cheerful man hauling an enormous trunk. "
     "Remember that guy."),
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
     "Touchdown in Wichita. Every hotel room in town is booked. Neal is stranded. "
     "Del, it turns out, knows a guy."),
    ("braidwood", at(TUE, 21, 0, CT),
     "The Braidwood Inn. Del got them a room. One room. One bed. Neal is now sharing a "
     "motel room with the man who stole his cab."),
    ("have-a-point", at(TUE, 22, 0, CT),
     "Neal finally snaps and unloads on Del: the stories, the noises, the shower curtain "
     "rings. \"Have a point!\" Del takes it quietly, then answers: he likes himself, and "
     "his wife likes him. Ouch."),
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
     "New plan: the train. Neal says his goodbyes and settles in for an uneventful ride "
     "to Chicago. Planes are over. Trains have begun."),
    ("breakdown", at(WED, 11, 30, CT),
     "The train breaks down in the middle of nowhere in Missouri. Passengers pile out and "
     "trudge across a field. Del, naturally, has found Neal again."),
    ("bus", at(WED, 13, 0, CT),
     "On a bus to St. Louis. Neal tries to lead a sing-along. It does not go well. "
     "Del takes over with the Flintstones theme and the whole bus joins in."),
    ("earrings", at(WED, 14, 30, CT),
     "St. Louis. Low on cash, Del starts selling his shower curtain rings to strangers as "
     "earrings. They're selling."),
    ("split", at(WED, 15, 30, CT),
     "After lunch, Neal and Del part ways. Neal heads off alone to rent a car and drive "
     "himself home. What could go wrong?"),
    ("rental-lot", at(WED, 16, 15, CT),
     "Neal rides the shuttle way out to the rental lot. The car isn't there. "
     "Nothing is there. He walks all the way back across the airport, slipping into a "
     "snowbank along the way."),
    ("rental-rant", at(WED, 17, 0, CT),
     "Back at the car rental counter, Neal unloads an extraordinarily profane rant on the "
     "cheerful agent. She lets him finish, then asks for his rental agreement. "
     "He doesn't have it."),
    ("dispatcher", at(WED, 17, 30, CT),
     "Neal mouths off to the airport cab dispatcher and gets punched in the face. "
     "Who pulls up to rescue him? Del Griffith, in his own rental car."),
    ("wrong-way", at(WED, 19, 15, CT),
     "Night on the interstate. Del is wrestling with his coat at the wheel. A couple across "
     "the median yells that they're going the wrong way. \"How would he know where we're "
     "going?\""),
    ("trucks", at(WED, 19, 20, CT),
     "They are, in fact, going the wrong way, squeezed between two oncoming semis. "
     "Neal looks over and sees the devil at the wheel. Somehow they survive. "
     "The car mostly does too."),
    ("fire", at(WED, 20, 15, CT),
     "Del's cigarette gets into the seat. The rental car goes up in flames. "
     "Del insists it still runs. Technically, it does."),
    ("motel2", at(WED, 21, 30, CT),
     "They limp into a motel in a charred car. The money is gone, so Neal barters for a room. "
     "Del ends up sitting outside in the frozen, burnt-out car."),
    ("come-in", at(WED, 22, 15, CT),
     "Neal can't stand it. He invites Del in from the cold. Out come the little bottles "
     "from the minibar, and for the first time, the two of them are actually laughing "
     "together."),
    ("thanksgiving", at(THU, 7, 30, CT),
     "Happy Thanksgiving. Neal and Del hit the road in what's left of the rental car. "
     "No windshield to speak of, no doors to speak of. Chicago is still a long way off."),
    ("trooper", at(THU, 9, 30, CT),
     "Pulled over by a state trooper. The car is impounded on the spot. "
     "Automobiles: done. They're stranded on the side of the road on Thanksgiving morning."),
    ("truck", at(THU, 10, 30, CT),
     "They hitch a ride in the back of a truck, bundled up in the cold, bumping their way "
     "toward Chicago."),
    ("chicago", at(THU, 15, 0, CT),
     "Chicago! Neal and Del finally make it. They say a warm goodbye at the train station "
     "and Neal hops on the L for the last leg home."),
    ("realization", at(THU, 15, 30, CT),
     "On the L, Neal thinks back over the last three days and something doesn't add up "
     "about Del. He gets off and rides back to the station."),
    ("marie", at(THU, 15, 50, CT),
     "Del is still there, sitting alone with his trunk. He admits he has no home to go to. "
     "His wife Marie passed away eight years ago."),
    ("home", at(THU, 16, 45, CT),
     "Neal walks through his front door with Del Griffith and his trunk. His wife Susan "
     "smiles. Del is staying for Thanksgiving dinner."),
    ("end", at(THU, 17, 0, CT),
     "Neal made it home for Thanksgiving. Planes, trains, automobiles, a bus, a truck, and "
     "a lot of shower curtain rings. Happy Thanksgiving from Neal, Del, and everyone at "
     "this account."),
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
