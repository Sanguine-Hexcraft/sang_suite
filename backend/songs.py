"""Song requests (Phase 13a): the queue, with no Twitch and no player yet.

A viewer will eventually type `!sr <youtube link>` (13c) and the song plays in
its own overlay (13b). This module is the part both of those lean on: turn
untrusted text into a validated video id, look the video up, apply the
channel's rules, and keep one authoritative queue.

Three ideas shape it:

- **Only an 11-character video id ever leaves this module, never the URL.** The
  overlay builds `youtube.com/embed/<id>` itself, so a crafted chat message
  can't point it anywhere else -- the twin of /ws only relaying `/media/` clips.
- **The queue lives here, on the backend**, unlike the alert queue (a display
  concern, so it lives in the overlay). Songs have several writers -- chat,
  the dashboard, the overlay reporting a song ended -- and that needs one
  copy everyone agrees on. It's saved to gitignored `songs.json` so a restart
  mid-stream doesn't throw away twenty requests.
- **Every check that costs nothing runs before the one that costs quota.** The
  lookup is 1 unit of a free 10,000/day, so garbage text, a full queue or a
  repeat requester is refused without ever calling YouTube.
"""

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import httpx

SONGS_PATH = Path(__file__).with_name("songs.json")

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


# --- parsing untrusted text --------------------------------------------------
# A video id is exactly 11 characters of URL-safe base64. Writing the class
# once keeps the URL pattern and the bare-id pattern from drifting apart.
_ID = r"[A-Za-z0-9_-]{11}"

# The link shapes people actually paste. Anchored at the start, and the id must
# not be followed by another id character, so `...v=abcdefghijkXYZ` (a 14-char
# "id") is refused rather than silently truncated to its first 11.
#   youtube.com/watch?v=ID (v= can come after other params: ?list=..&v=ID)
#   youtu.be/ID   /shorts/ID   /embed/ID   /live/ID
#   with or without https://, www., m. (mobile) or music.
_URL = re.compile(
    r"^(?:https?://)?(?:(?:www|m|music)\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    rf"({_ID})(?![A-Za-z0-9_-])"
)
_BARE_ID = re.compile(rf"^{_ID}$")


def parse_video_id(text: str) -> str | None:
    """The video id in a request, or None if it isn't a YouTube link.

    Only the first word is considered, so `!sr <link> this one slaps` works.
    """
    words = text.split()
    if not words:
        return None
    word = words[0].strip("<>")  # people paste Discord-style <link>s
    if m := _URL.match(word):
        return m.group(1)
    if _BARE_ID.match(word):
        return word
    return None


# ISO 8601 durations as the Data API writes them: PT4M13S, PT1H2M, P1DT3H.
# A live stream reports P0D. Every part is optional, so this also matches an
# empty "P"/"PT" -- which correctly reads as zero.
_DURATION = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_duration(iso: str) -> int | None:
    """`PT1H4M13S` -> 3853 seconds. None if it isn't a duration we understand."""
    m = _DURATION.match(iso)
    if not m:
        return None
    d, h, mins, s = (int(g) if g else 0 for g in m.groups())
    return ((d * 24 + h) * 60 + mins) * 60 + s


# --- the YouTube lookup ------------------------------------------------------
@dataclass
class VideoInfo:
    title: str
    channel: str
    channel_id: str
    duration_s: int
    embeddable: bool
    live: bool


async def lookup(video_id: str, api_key: str) -> tuple[VideoInfo | None, str | None]:
    """Ask the Data API about one video. Returns (info, None) or (None, reason).

    One `videos.list` call answers every question the queue needs -- title and
    channel for the reply, duration for the cap, embeddable so we refuse now
    instead of failing on stream, live so a stream that never ends can't
    wedge the queue. Never raises: a YouTube outage refuses one request, it
    doesn't take the endpoint down.
    """
    params = {
        "part": "snippet,contentDetails,status",
        "id": video_id,
        "key": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(YOUTUBE_VIDEOS_URL, params=params)
    except httpx.HTTPError as exc:
        print(f"[songs] lookup failed for {video_id}: {exc}")
        return None, "couldn't reach YouTube, try again in a bit"

    if res.status_code != 200:
        # 400 = bad key, 403 = quota exhausted or the key isn't allowed this
        # API. Logged in full for you; the viewer gets a generic line, since
        # the API's error text can mention the key's project.
        print(f"[songs] lookup {video_id} -> HTTP {res.status_code}: {res.text[:300]}")
        return None, "YouTube refused the lookup, try again later"

    items = res.json().get("items", [])
    if not items:
        # Private, deleted, or a well-formed id that was never a video. All the
        # same to a viewer.
        return None, "couldn't find that video (private or removed?)"

    v = items[0]
    snippet = v.get("snippet", {})
    duration = parse_duration(v.get("contentDetails", {}).get("duration", ""))
    return VideoInfo(
        title=snippet.get("title", "(untitled)"),
        channel=snippet.get("channelTitle", "?"),
        channel_id=snippet.get("channelId", ""),
        duration_s=duration or 0,
        embeddable=bool(v.get("status", {}).get("embeddable", False)),
        # "live" while streaming, "upcoming" for a scheduled premiere; both
        # are unplayable as a song. "none" is an ordinary video.
        live=snippet.get("liveBroadcastContent", "none") != "none",
    ), None


# --- the queue ---------------------------------------------------------------
@dataclass
class SongRequest:
    # Unique per *request*, not per video: the same song can be requested twice
    # in a night. 13b's overlay quotes it back when a song ends, so a stale or
    # duplicate "ended" from a second open overlay can be told apart and
    # ignored instead of skipping the next song.
    request_id: str
    video_id: str
    title: str
    channel: str
    channel_id: str
    duration_s: int
    # Display name for the card and replies; user_id is what every rule keys
    # on, because names change case and get changed outright.
    user: str
    user_id: str
    requested_at: int  # ms since epoch, like the activity feed's `at`


class SongRules(Protocol):
    """The slice of main.py's SongConfig this module reads.

    A Protocol rather than an import so songs.py doesn't depend on main.py,
    which imports it.
    """

    enabled: bool
    max_duration_s: int
    max_queue: int
    per_user_limit: int
    blocklist: list[str]


def _fmt_duration(seconds: int) -> str:
    """4:13, or 1:04:13 past an hour. Days fold into hours: an ended 24/7
    stream comes back as one enormous ordinary video (P1407DT10H...), and
    "33778:11:52" at least reads as absurd, where minutes alone looked broken."""
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


class SongQueue:
    def __init__(
        self,
        broadcast: Callable[[dict], Awaitable[None]],
        get_rules: Callable[[], SongRules],
    ):
        self._broadcast = broadcast
        # A callable, not the config object itself, for the same reason as
        # main.py's _reward_lookup: PUT /api/config rebinds `config` wholesale,
        # so a captured object would keep enforcing the old rules forever.
        self._get_rules = get_rules
        # `now` is the song the overlay should be playing; `queue` is what's
        # waiting, in order. Kept apart rather than as queue[0], so "skip" and
        # "what's playing" never have to wonder whether index 0 has started.
        self.now: SongRequest | None = None
        self.queue: list[SongRequest] = []
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        """Restore the queue at startup. A broken file starts empty, not dead.

        Playback position is deliberately not saved: a restarted song simply
        starts from the top.
        """
        if not SONGS_PATH.exists():
            return
        try:
            saved = json.loads(SONGS_PATH.read_text())
            self.now = SongRequest(**saved["now"]) if saved.get("now") else None
            self.queue = [SongRequest(**r) for r in saved.get("queue", [])]
        except (ValueError, OSError, KeyError, TypeError) as exc:
            print(f"[songs] {SONGS_PATH.name} unusable, starting empty: {exc}")
            self.now, self.queue = None, []

    def _save(self) -> None:
        try:
            # Written to a temp file and renamed into place: rename is atomic,
            # so a crash mid-write leaves the old queue rather than half a file.
            tmp = SONGS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state(), indent=2) + "\n")
            os.replace(tmp, SONGS_PATH)
        except OSError as exc:
            print(f"[songs] Couldn't write {SONGS_PATH.name}: {exc}")

    async def _changed(self) -> None:
        """Every mutation ends here: persist, then tell the overlays.

        `songs` is state, not an event -- main.py keeps it out of the replay
        buffer and the activity feed, the way it does `config` and `deaths`.
        A reconnecting overlay asks GET /api/songs instead.
        """
        self._save()
        await self._broadcast({"type": "songs", **self.state()})

    def state(self) -> dict:
        return {
            "now": asdict(self.now) if self.now else None,
            "queue": [asdict(r) for r in self.queue],
        }

    # --- requests -----------------------------------------------------------
    def _room_for(self, rules: SongRules, video_id: str, user_id: str) -> str | None:
        """The checks that depend on the queue's contents. None = fits.

        Run twice: before the lookup, to avoid spending quota on a request
        that can't fit; and again after, because the lookup awaits, and while
        it does another request can arrive and take the last slot. Checking
        only before would let thirty simultaneous `!sr`s all squeeze past a
        twenty-song cap.
        """
        if any(r.video_id == video_id for r in self._all()):
            return "that song is already in the queue"
        # Counts songs *waiting*, not the one playing: once your song is on,
        # you can queue the next. That's what viewers expect "one each" to mean.
        mine = sum(1 for r in self.queue if r.user_id == user_id)
        if mine >= rules.per_user_limit:
            return f"you already have {mine} song(s) waiting, try again once it plays"
        if len(self.queue) >= rules.max_queue:
            return "the queue is full"
        return None

    def _all(self) -> list[SongRequest]:
        return ([self.now] if self.now else []) + self.queue

    async def request(
        self, text: str, user: str, user_id: str
    ) -> tuple[SongRequest | None, str | None]:
        """Try to queue `text` for `user`. Returns (request, None) or (None, reason).

        The one entry point for every source: POST /api/songs/request now,
        `!sr` in chat later -- identical code path, so the dashboard test
        proves the chat command. Each reason is a short sentence the bot can
        say straight back to the viewer.
        """
        rules = self._get_rules()
        if not rules.enabled:
            return None, "song requests are closed right now"

        video_id = parse_video_id(text)
        if video_id is None:
            return None, "that isn't a YouTube link"
        if video_id in rules.blocklist:
            return None, "that song isn't allowed here"
        if reason := self._room_for(rules, video_id, user_id):
            return None, reason

        # Read at call time, not import time: main.py loads .env after
        # importing this module. Without a key we refuse rather than queue
        # unchecked -- unchecked means no length cap and no embeddable check,
        # which is how a three-hour video ends up on stream.
        api_key = os.getenv("YOUTUBE_API_KEY")
        if not api_key:
            print("[songs] YOUTUBE_API_KEY not set in backend/.env — refusing requests.")
            return None, "song requests are unavailable right now"

        info, reason = await lookup(video_id, api_key)
        if info is None:
            return None, reason
        if info.live:
            return None, "live streams can't be requested"
        if not info.embeddable:
            return None, "that video can't be played outside YouTube"
        if info.channel_id in rules.blocklist:
            return None, "songs from that channel aren't allowed here"
        if info.duration_s <= 0:
            return None, "couldn't tell how long that video is"
        if info.duration_s > rules.max_duration_s:
            return None, (
                f"that's {_fmt_duration(info.duration_s)} long, "
                f"the limit is {_fmt_duration(rules.max_duration_s)}"
            )
        # Second pass: the world may have moved while we awaited YouTube.
        # Rules re-read too, in case requests were closed meanwhile.
        rules = self._get_rules()
        if not rules.enabled:
            return None, "song requests are closed right now"
        if reason := self._room_for(rules, video_id, user_id):
            return None, reason

        req = SongRequest(
            request_id=uuid.uuid4().hex,
            video_id=video_id,
            title=info.title,
            channel=info.channel,
            channel_id=info.channel_id,
            duration_s=info.duration_s,
            user=user,
            user_id=user_id,
            requested_at=int(time.time() * 1000),
        )
        # Nothing playing? It goes straight on. Actually *playing* it is the
        # overlay's job (13b); the queue only decides what should be on.
        if self.now is None:
            self.now = req
        else:
            self.queue.append(req)
        await self._changed()
        return req, None

    def position(self, request_id: str) -> int | None:
        """0 = playing now, 1 = next up, ... None if it isn't queued."""
        for i, r in enumerate(self._all()):
            if r.request_id == request_id:
                return i
        return None

    # --- control --------------------------------------------------------------
    async def skip(self) -> SongRequest | None:
        """Drop what's playing and promote the next song. Returns the new `now`."""
        self.now = self.queue.pop(0) if self.queue else None
        await self._changed()
        return self.now

    async def remove(self, request_id: str) -> bool:
        """Remove one request, wherever it is. Removing `now` is a skip."""
        if self.now and self.now.request_id == request_id:
            await self.skip()
            return True
        before = len(self.queue)
        self.queue = [r for r in self.queue if r.request_id != request_id]
        if len(self.queue) == before:
            return False
        await self._changed()
        return True
