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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Protocol

import httpx

SONGS_PATH = Path(__file__).with_name("songs.json")

# Played songs kept for the dashboard's history list. They ride along on every
# `songs` broadcast, so this is a cap on message size as much as on the list.
HISTORY_LIMIT = 50

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# How many search results to consider. All of them are checked in one
# videos.list call (1 unit however many ids), and the first that passes every
# rule wins. More than one matters: the top hit for a famous song is often
# the label's official video, which frequently refuses embedding, while the
# lyric or audio upload a place or two down plays fine.
SEARCH_CANDIDATES = 5
# Google resets the daily quota at midnight Pacific time, wherever you are, so
# the search budget's "day" has to be that day too.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")


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
    video_id: str
    title: str
    channel: str
    channel_id: str
    duration_s: int
    embeddable: bool
    live: bool


async def _youtube_get(
    url: str, params: dict, what: str
) -> tuple[dict | None, str | None]:
    """GET a Data API endpoint. Returns (json, None) or (None, reason).

    Never raises: a YouTube outage refuses one request, it doesn't take the
    endpoint down.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        print(f"[songs] {what} failed: {exc}")
        return None, "couldn't reach YouTube, try again in a bit"
    if res.status_code != 200:
        # 400 = bad key, 403 = quota exhausted or the key isn't allowed this
        # API. Logged in full for you; the viewer gets a generic line, since
        # the API's error text can mention the key's project.
        print(f"[songs] {what} -> HTTP {res.status_code}: {res.text[:300]}")
        return None, "YouTube refused the lookup, try again later"
    return res.json(), None


async def lookup(
    video_ids: list[str], api_key: str
) -> tuple[list[VideoInfo], str | None]:
    """Ask the Data API about up to 50 videos in one call (1 quota unit).

    Returns (infos, None) in the order the ids were given, skipping any that
    don't exist; or ([], reason) if YouTube couldn't be asked at all.

    One `videos.list` call answers every question the queue needs -- title and
    channel for the reply, duration for the cap, embeddable so we refuse now
    instead of failing on stream, live so a stream that never ends can't
    wedge the queue.
    """
    params = {
        "part": "snippet,contentDetails,status",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    data, reason = await _youtube_get(YOUTUBE_VIDEOS_URL, params, f"lookup {video_ids}")
    if data is None:
        return [], reason

    found: dict[str, VideoInfo] = {}
    for v in data.get("items", []):
        snippet = v.get("snippet", {})
        duration = parse_duration(v.get("contentDetails", {}).get("duration", ""))
        found[v["id"]] = VideoInfo(
            video_id=v["id"],
            title=snippet.get("title", "(untitled)"),
            channel=snippet.get("channelTitle", "?"),
            channel_id=snippet.get("channelId", ""),
            duration_s=duration or 0,
            embeddable=bool(v.get("status", {}).get("embeddable", False)),
            # "live" while streaming, "upcoming" for a scheduled premiere;
            # both are unplayable as a song. "none" is an ordinary video.
            live=snippet.get("liveBroadcastContent", "none") != "none",
        )
    # The API doesn't promise to answer in the order asked, and for search
    # results that order is the ranking, so put it back.
    return [found[i] for i in video_ids if i in found], None


async def search(query: str, api_key: str) -> tuple[list[str], str | None]:
    """Search YouTube for a song. Returns (video ids, best first) or ([], reason).

    **Costs 100 quota units**, against 1 for a lookup -- the whole reason the
    search path has a daily budget. Only ids come back; the caller looks them
    all up in one more call, since search results carry no duration.
    """
    params = {
        "part": "id",
        "q": query,
        "type": "video",
        "maxResults": SEARCH_CANDIDATES,
        # Filters YouTube applies for free, so fewer candidates are wasted:
        # only embeddable videos, only the Music category, and nothing the
        # strictest safe search would hide -- it's searching on stream.
        "videoEmbeddable": "true",
        "videoCategoryId": "10",
        "safeSearch": "strict",
        "key": api_key,
    }
    data, reason = await _youtube_get(YOUTUBE_SEARCH_URL, params, f"search {query!r}")
    if data is None:
        return [], reason
    ids = [
        item["id"]["videoId"]
        for item in data.get("items", [])
        if item.get("id", {}).get("videoId")
    ]
    return ids, None


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
    # The chat message that asked for it (13c), so a mod deleting that message
    # can pull the request. None for dashboard requests. Defaulted so a
    # songs.json saved before this field still loads.
    message_id: str | None = None
    # When it became `now` (ms). Set on promotion, None while waiting.
    started_at: int | None = None


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
    search_enabled: bool
    search_daily_budget: int
    search_cooldown_s: int


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
        # Music stopped on purpose (the panic button, or Pause on the
        # dashboard). A flag on the queue rather than an event, because it's
        # state: a browser source that reconnects or reloads mid-pause must
        # come back paused, not start blaring the song again. Saved for the
        # same reason across a restart.
        self.paused = False
        # Songs that have left `now`, newest first, each a SongRequest dict
        # plus `ended_at`, `outcome` and `detail`. Kept as plain dicts: it's a
        # record, nothing ever acts on it again.
        self.history: list[dict] = []
        # Searches spent today, against config's daily budget. `search_day` is
        # the Pacific date they were counted on (see QUOTA_TZ); a new day
        # means a fresh count. Saved, so a restart can't hand out a second
        # day's worth of searches.
        self.search_day = ""
        self.searches_used = 0
        # user_id -> monotonic time of their last search. Not saved: a restart
        # resetting a two-minute cooldown costs nothing.
        self._searched: dict[str, float] = {}
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
            self.paused = bool(saved.get("paused", False))
            self.history = list(saved.get("history", []))[:HISTORY_LIMIT]
            searches = saved.get("searches") or {}
            self.search_day = str(searches.get("day", ""))
            self.searches_used = int(searches.get("used", 0))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            print(f"[songs] {SONGS_PATH.name} unusable, starting empty: {exc}")
            self.now, self.queue, self.paused, self.history = None, [], False, []

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
            "paused": self.paused,
            "history": self.history,
            "searches": {"day": self.search_day, "used": self._searches_today()},
        }

    @staticmethod
    def _quota_day() -> str:
        return datetime.now(QUOTA_TZ).date().isoformat()

    def _searches_today(self) -> int:
        return self.searches_used if self.search_day == self._quota_day() else 0

    # --- requests -----------------------------------------------------------
    def _room_for(
        self, rules: SongRules, video_id: str | None, user_id: str
    ) -> str | None:
        """The checks that depend on the queue's contents. None = fits.

        Run twice: before the lookup, to avoid spending quota on a request
        that can't fit; and again after, because the lookup awaits, and while
        it does another request can arrive and take the last slot. Checking
        only before would let thirty simultaneous `!sr`s all squeeze past a
        twenty-song cap. `video_id` is None before a search, when there's no
        video yet to check for duplicates.
        """
        if video_id and any(r.video_id == video_id for r in self._all()):
            return "that song is already in the queue"
        # Counts songs *waiting*, not the one playing: once your song is on,
        # you can queue the next. That's what viewers expect "one each" to mean.
        mine = sum(1 for r in self.queue if r.user_id == user_id)
        if mine >= rules.per_user_limit:
            return f"you already have {mine} song(s) waiting, try again once it plays"
        if len(self.queue) >= rules.max_queue:
            return "the queue is full"
        return None

    @staticmethod
    def _refusal(info: VideoInfo, rules: SongRules) -> str | None:
        """The rules about the video itself. None = playable here.

        Shared by links and search: a link gets this reason back, while a
        search just moves on to its next candidate.
        """
        if info.live:
            return "live streams can't be requested"
        if not info.embeddable:
            return "that video can't be played outside YouTube"
        if info.video_id in rules.blocklist:
            return "that song isn't allowed here"
        if info.channel_id in rules.blocklist:
            return "songs from that channel aren't allowed here"
        if info.duration_s <= 0:
            return "couldn't tell how long that video is"
        if info.duration_s > rules.max_duration_s:
            return (
                f"that's {_fmt_duration(info.duration_s)} long, "
                f"the limit is {_fmt_duration(rules.max_duration_s)}"
            )
        return None

    def _all(self) -> list[SongRequest]:
        return ([self.now] if self.now else []) + self.queue

    async def request(
        self, text: str, user: str, user_id: str, message_id: str | None = None
    ) -> tuple[SongRequest | None, str | None]:
        """Try to queue `text` for `user`. Returns (request, None) or (None, reason).

        The one entry point for every source -- the dashboard and `!sr` run
        identical code, so testing one proves the other. `text` is a link (or
        a bare id), or failing that a search. Each reason is a short sentence
        the bot can say straight back to the viewer.
        """
        rules = self._get_rules()
        if not rules.enabled:
            return None, "song requests are closed right now"

        video_id = parse_video_id(text)
        if video_id is None:
            return await self._request_by_search(text, user, user_id, message_id, rules)

        if video_id in rules.blocklist:
            return None, "that song isn't allowed here"
        if reason := self._room_for(rules, video_id, user_id):
            return None, reason
        api_key = self._api_key()
        if not api_key:
            return None, "song requests are unavailable right now"

        infos, reason = await lookup([video_id], api_key)
        if reason:
            return None, reason
        if not infos:
            # Private, deleted, or a well-formed id that was never a video.
            # All the same to a viewer.
            return None, "couldn't find that video (private or removed?)"
        if reason := self._refusal(infos[0], rules):
            return None, reason
        return await self._add(infos[0], user, user_id, message_id)

    async def _request_by_search(
        self,
        text: str,
        user: str,
        user_id: str,
        message_id: str | None,
        rules: SongRules,
    ) -> tuple[SongRequest | None, str | None]:
        """`!sr <title and/or artist>`. 100 quota units, so guarded hard.

        Every free check runs before the search: a request that would be
        refused anyway must not spend a hundredth of the day's quota first.
        """
        if not rules.search_enabled:
            return None, "that isn't a YouTube link"
        # Collapse whitespace and cap the length: it's viewer text going into
        # a URL, and nobody's song name needs more than this.
        query = " ".join(text.split())[:100]
        if not query:
            return None, "that isn't a YouTube link"
        if reason := self._room_for(rules, None, user_id):
            return None, reason
        api_key = self._api_key()
        if not api_key:
            return None, "song requests are unavailable right now"

        # The budget protects links as much as searches: links share the same
        # daily quota, so one viewer spamming searches could otherwise leave
        # nobody able to request anything until Google's midnight reset.
        if self.search_day != self._quota_day():
            self.search_day, self.searches_used = self._quota_day(), 0
        if self.searches_used >= rules.search_daily_budget:
            return None, "song search is used up for today, but links still work"
        since = time.monotonic() - self._searched.get(user_id, float("-inf"))
        if since < rules.search_cooldown_s:
            wait = int(rules.search_cooldown_s - since) + 1
            return None, f"you can search again in {wait}s (links work anytime)"

        # Spent the moment we ask: Google charges for the call whether or not
        # anything playable comes back.
        self.searches_used += 1
        self._searched[user_id] = time.monotonic()
        await self._changed()  # saves the count, and the dashboard shows it

        ids, reason = await search(query, api_key)
        if reason:
            return None, reason
        if not ids:
            return None, f"no results for {query!r}"
        infos, reason = await lookup(ids, api_key)
        if reason:
            return None, reason
        queued = {r.video_id for r in self._all()}
        for info in infos:
            if info.video_id not in queued and not self._refusal(info, rules):
                return await self._add(info, user, user_id, message_id)
        if any(info.video_id in queued for info in infos):
            return None, "that song is already in the queue"
        return None, f"couldn't find a playable match for {query!r}, try a link"

    @staticmethod
    def _api_key() -> str | None:
        # Read at call time, not import time: main.py loads .env after
        # importing this module. Without a key we refuse rather than queue
        # unchecked -- unchecked means no length cap and no embeddable check,
        # which is how a three-hour video ends up on stream.
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            print("[songs] YOUTUBE_API_KEY not set in backend/.env — refusing requests.")
        return key

    async def _add(
        self, info: VideoInfo, user: str, user_id: str, message_id: str | None
    ) -> tuple[SongRequest | None, str | None]:
        """Queue a video that has passed every check. Shared by link and search."""
        # Second pass: the world may have moved while we awaited YouTube.
        # Rules re-read too, in case requests were closed meanwhile.
        rules = self._get_rules()
        if not rules.enabled:
            return None, "song requests are closed right now"
        if reason := self._room_for(rules, info.video_id, user_id):
            return None, reason

        req = SongRequest(
            request_id=uuid.uuid4().hex,
            video_id=info.video_id,
            title=info.title,
            channel=info.channel,
            channel_id=info.channel_id,
            duration_s=info.duration_s,
            user=user,
            user_id=user_id,
            requested_at=int(time.time() * 1000),
            message_id=message_id,
        )
        # Nothing playing? It goes straight on. Actually *playing* it is the
        # overlay's job; the queue only decides what should be on.
        if self.now is None:
            self._promote(req)
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
    def _promote(self, req: SongRequest | None) -> None:
        if req is not None:
            req.started_at = int(time.time() * 1000)
        self.now = req

    async def skip(
        self, outcome: str = "skipped", detail: str | None = None
    ) -> SongRequest | None:
        """Drop what's playing and promote the next song. Returns the new `now`.

        Every way a song leaves `now` comes through here -- it finished,
        someone skipped it, YouTube refused it, it was removed -- which makes
        this the one place to write the history. `outcome` is one of
        played / skipped / failed / removed; `detail` says why it failed.
        """
        if self.now is not None:
            entry = {
                **asdict(self.now),
                "ended_at": int(time.time() * 1000),
                "outcome": outcome,
                "detail": detail,
            }
            self.history = [entry, *self.history][:HISTORY_LIMIT]
        self._promote(self.queue.pop(0) if self.queue else None)
        await self._changed()
        return self.now

    async def ended(self, request_id: str, reason: str) -> bool:
        """The player says a song is over. Advance only if it's the current one.

        The player quotes the request_id it was playing rather than just saying
        "next", because more than one copy of the overlay can be open (OBS plus
        a browser tab you forgot about). Each reports the same song ending; the
        first one advances the queue and every later one names a song that is
        no longer `now`, so it's ignored instead of skipping the next song too.
        """
        if self.now is None or self.now.request_id != request_id:
            return False
        if reason != "ended":
            # An embed refusal or a stall. Worth a line in the terminal: a
            # pattern of 150s from one label tells you what to blocklist.
            print(f"[songs] skipped {self.now.title!r} ({self.now.video_id}): {reason}")
            await self.skip("failed", reason)
        else:
            await self.skip("played")
        return True

    async def set_paused(self, paused: bool) -> None:
        if self.paused == paused:
            return
        self.paused = paused
        await self._changed()

    def mine(self, user_id: str) -> list[SongRequest]:
        """This viewer's requests, playing one first, then in queue order."""
        return [r for r in self._all() if r.user_id == user_id]

    async def remove_by_message(self, message_id: str) -> SongRequest | None:
        """A mod deleted a chat message; pull the request it made, if any."""
        for r in self._all():
            if r.message_id == message_id:
                await self.remove(r.request_id)
                return r
        return None

    async def remove(self, request_id: str) -> bool:
        """Remove one request, wherever it is. Removing `now` is a skip."""
        if self.now and self.now.request_id == request_id:
            await self.skip("removed")
            return True
        before = len(self.queue)
        self.queue = [r for r in self.queue if r.request_id != request_id]
        if len(self.queue) == before:
            return False
        await self._changed()
        return True
