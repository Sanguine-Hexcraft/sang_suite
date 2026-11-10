"""Spotify "now playing" (Phase 14b): what the desktop app is playing, via MPRIS.

No Spotify API, no OAuth, no key. Every Linux media player announces itself on
the session D-Bus under a standard called MPRIS -- it's what the desktop's
media widget and the keyboard's play/pause key talk to. Spotify owns the name
`org.mpris.MediaPlayer2.spotify` while it's running, and one `GetAll` call on
it returns the track, the artwork URL, play/pause and the position.

Three ideas shape this module:

- **Spotify not running is the normal case, not an error.** The bus answers
  "no such name", and that just means nothing is playing -- the same way the
  backend boots fine without OBS.
- **Only changes are broadcast.** The loop looks every second, but a `spotify`
  message goes out only for a new track, a play/pause or a seek. The progress
  bar isn't fed once a second: each message carries the position, and the
  overlay advances it on its own clock from there.
- **Song requests win.** When a viewer's YouTube request starts, Spotify is
  paused, and resumed once the request queue runs dry -- but only if *we* were
  the ones who paused it. See `_handoff`.
"""

import asyncio
import time
from typing import Callable

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

from mnm import Broadcast

POLL_SECONDS = 1.0

BUS_NAME = "org.mpris.MediaPlayer2.spotify"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Album art is the one URL in the payload that the overlay will actually
# fetch. Spotify's CDN is the only place it may point: the twin of /ws only
# relaying `/media/` clips. (A local file's art is a file:// path, which a
# browser source couldn't load anyway.)
ART_PREFIX = "https://i.scdn.co/"

# How far the position may drift from where the last broadcast said it would
# be before we call it a seek. Poll timing jitters by a few hundred ms.
SEEK_TOLERANCE_MS = 2_000

# What `SpotifyWatcher` needs to know about the request queue: the request_id
# of the song that is audibly playing (None if there isn't one), and whether
# the queue is empty and free to hand the speakers back.
AudibleRequest = Callable[[], str | None]
QueueIdle = Callable[[], bool]


def _plain(value):
    """Strip dbus-fast's Variant wrappers: a{sv} arrives as {str: Variant}."""
    return getattr(value, "value", value)


def parse_player(props: dict) -> dict:
    """Turn MPRIS Player properties into the state the overlay gets.

    Everything here is read defensively with .get(): MPRIS makes most
    metadata optional, and Spotify briefly serves an empty dict between
    tracks.
    """
    meta = {k: _plain(v) for k, v in (_plain(props.get("Metadata")) or {}).items()}
    track_id = str(meta.get("mpris:trackid") or "")
    title = str(meta.get("xesam:title") or "")
    art = str(meta.get("mpris:artUrl") or "")
    track = None
    # An ad has a track id like /com/spotify/ad/...; nobody wants
    # "Advertisement" on the overlay, so it counts as no track at all.
    if title and "/ad/" not in track_id:
        track = {
            "id": track_id,
            "title": title,
            "artists": [str(a) for a in meta.get("xesam:artist") or [] if a],
            "album": str(meta.get("xesam:album") or ""),
            "art": art if art.startswith(ART_PREFIX) else None,
            # MPRIS speaks microseconds; everything else here speaks ms.
            "duration_ms": int(meta.get("mpris:length") or 0) // 1000,
        }
    return {
        "running": True,
        "playing": _plain(props.get("PlaybackStatus")) == "Playing",
        "track": track,
        "position_ms": int(_plain(props.get("Position")) or 0) // 1000,
    }


STOPPED = {"running": False, "playing": False, "track": None, "position_ms": 0}


class SpotifyWatcher:
    """Polls Spotify over MPRIS, broadcasts changes, yields to song requests."""

    def __init__(self, broadcast: Broadcast, audible_request: AudibleRequest, queue_idle: QueueIdle):
        self._broadcast = broadcast
        self._audible_request = audible_request
        self._queue_idle = queue_idle
        self._task: asyncio.Task | None = None
        self._bus: MessageBus | None = None
        self._bus_warned = False
        # Set by nudge() to cut the sleep short, so a request starting pauses
        # Spotify now rather than up to a second from now.
        self._wake = asyncio.Event()
        # The last state read, and the monotonic time it was read at -- which
        # is what lets snapshot() and the seek check work out where the
        # position should be by now.
        self._state: dict = dict(STOPPED)
        self._read_at = time.monotonic()
        # The last state *broadcast*, and when: what the overlays believe.
        self._sent: dict | None = None
        self._sent_at = 0.0
        # Handoff bookkeeping -- see _handoff.
        self._last_request: str | None = None
        self._we_paused = False

    # --- talking to the bus -----------------------------------------------------
    async def _call(self, interface: str, member: str, signature: str = "", body: list | None = None):
        """One method call on Spotify. Returns the reply body, or None if
        Spotify isn't there. Raises only if the bus itself is unusable."""
        if self._bus is None or not self._bus.connected:
            # The session bus, found through DBUS_SESSION_BUS_ADDRESS. Kept
            # open between polls; reopened if the connection ever drops.
            self._bus = await MessageBus().connect()
            self._bus_warned = False
        reply = await self._bus.call(
            Message(
                destination=BUS_NAME,
                path=OBJECT_PATH,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            # Nearly always ServiceUnknown: Spotify is closed.
            return None
        return reply.body

    async def _read(self) -> dict:
        body = await self._call(PROPS_IFACE, "GetAll", "s", [PLAYER_IFACE])
        return parse_player(body[0]) if body else dict(STOPPED)

    # --- what the overlays see --------------------------------------------------
    @staticmethod
    def _advance(state: dict, since: float) -> int:
        """Where `state`'s position has got to, `since` being when it was read."""
        if not state["playing"]:
            return state["position_ms"]
        return state["position_ms"] + int((time.monotonic() - since) * 1000)

    def snapshot(self) -> dict:
        """Current state for GET /api/spotify, position brought up to now.

        The contract with the overlay, for this and for broadcasts alike:
        `position_ms` is true *at the moment the message was sent*. The
        overlay notes when it arrived and counts forward from there, so no
        wall-clock time ever crosses the wire.
        """
        return {**self._state, "position_ms": self._advance(self._state, self._read_at)}

    def _differs(self, state: dict) -> bool:
        """Is `state` news to the overlays, or just the same song a bit later?"""
        sent = self._sent
        if sent is None:
            return True
        if (state["running"], state["playing"], state["track"]) != (
            sent["running"],
            sent["playing"],
            sent["track"],
        ):
            return True
        # Same track, same play state: the only news left is a seek, i.e. the
        # position isn't where the last message implied it would be by now.
        expected = self._advance(sent, self._sent_at)
        return abs(state["position_ms"] - expected) > SEEK_TOLERANCE_MS

    # --- yielding to song requests ------------------------------------------------
    async def _handoff(self, state: dict) -> None:
        """Pause Spotify for a song request; give it back afterwards.

        Edge-triggered, on purpose. Spotify is paused once, when a request
        *becomes* audible -- not held down for as long as one is playing. If
        you press play in Spotify yourself mid-request, that's your call and
        it stays playing; a loop that re-paused every second would be a fight
        you can't win from the keyboard.

        `_we_paused` is the other half: Spotify is only resumed if this code
        paused it. Without that, the end of a request would start Spotify
        playing on a stream where you'd deliberately left it off.
        """
        request = self._audible_request()
        if request and request != self._last_request and state["playing"]:
            if await self._call(PLAYER_IFACE, "Pause") is not None:
                self._we_paused = True
                print("[spotify] Paused for a song request")
        self._last_request = request

        if self._we_paused and not state["running"]:
            # Spotify was closed while we held it. Nothing left to resume.
            self._we_paused = False
        elif self._we_paused and self._queue_idle():
            # Idle means empty *and* not paused: after a panic the queue is
            # paused, and music coming back on its own would be exactly what
            # the panic button was pressed to stop. Resume brings it back.
            self._we_paused = False
            # Still paused? Then it's ours to restart. If it's playing, you
            # already restarted it yourself.
            if not state["playing"]:
                await self._call(PLAYER_IFACE, "Play")
                print("[spotify] Request queue empty, resumed")

    # --- the loop ---------------------------------------------------------------
    def nudge(self) -> None:
        """Look now instead of at the next tick. Called when the queue changes."""
        self._wake.set()

    async def _tick(self) -> None:
        state = await self._read()
        await self._handoff(state)
        self._state, self._read_at = state, time.monotonic()
        if self._differs(state):
            self._sent, self._sent_at = state, self._read_at
            await self._broadcast({"type": "spotify", **state})

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._tick()
            except Exception as exc:  # never let one bad pass kill the loop
                # Almost certainly no session bus (started from a bare TTY or
                # a service). Said once, not every second; retried quietly.
                if not self._bus_warned:
                    print(f"[spotify] Can't reach the session D-Bus, will keep trying: {exc}")
                    self._bus_warned = True
                self._bus = None
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
