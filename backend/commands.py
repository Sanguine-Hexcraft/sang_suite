"""Chat commands (Phase 13c): chat becomes an input.

Until now every input was you (the dashboard), Twitch acting for a viewer
(follows, subs, redeems) or the game. A chat message is arbitrary text from
anyone, so this module's first job is deciding what to ignore -- in this
order, before any command is even parsed:

1. **Shared Chat.** Messages from another channel in a Shared Chat session
   come from that streamer's viewers, not yours.
2. **Loop prevention.** Never act on our own bot (by user id), or on any login
   in the bot denylist (Nightbot, StreamElements, ...). Otherwise two bots
   end up answering each other on stream at 2am.
3. **Not a command.** Anything not starting with `!` is ignored for now (loot
   boxes, Phase 15, will want every message -- they'll hook in here too).

Then permissions come from the message's own badges, never extra API calls.

Replies go through an injected `reply` callable rather than straight to the
bot. Real chat replies as a threaded message; POST /api/chat/test collects the
replies and returns them instead, so every command is testable from the
dashboard without a second Twitch account -- and without the bot posting
test noise into your real chat.
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from songs import SongQueue, parse_video_id
from twitch import ChatLine

if TYPE_CHECKING:  # main.py imports this module
    from main import Config

Reply = Callable[[str], Awaitable[None]]
# (text, reply_to message id) -> (sent, reason). ChatBot.send's shape.
Send = Callable[[str, str | None], Awaitable[tuple[bool, str | None]]]

MOD_BADGES = frozenset({"broadcaster", "moderator", "lead_moderator"})
VIP_BADGES = MOD_BADGES | {"vip"}
# Founders are early subscribers who show a founder badge *instead* of the
# subscriber one, so they'd be locked out of sub-only requests without this.
SUB_BADGES = VIP_BADGES | {"subscriber", "founder"}

LURK_GIF = Path(__file__).parent / "media" / "gifs" / "lurky.gif"
# Per viewer. A gif on stream is a cost to everyone watching, so one viewer
# typing !lurk ten times shouldn't put it up ten times.
LURK_COOLDOWN_S = 10 * 60

# Twitch caps a message at 500 characters; YouTube titles can run to 100 and
# usually carry a "| NCS - Copyright Free Music" tail nobody needs read out.
TITLE_MAX = 80


def _short(title: str) -> str:
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"


class ChatCommands:
    def __init__(
        self,
        songs: SongQueue,
        send: Send,
        broadcast: Callable[[dict], Awaitable[None]],
        get_config: Callable[[], "Config"],
        bot_user_id: Callable[[], str | None],
    ):
        self._songs = songs
        self._send = send
        self._broadcast = broadcast
        # Callables, for the same stale-capture reason as everywhere else:
        # config is rebound by every dashboard save, and the bot's id is only
        # known once it has logged in, after this object exists.
        self._get_config = get_config
        self._bot_user_id = bot_user_id
        self._lurked: dict[str, float] = {}
        self._commands: dict[str, Callable[[ChatLine, str, Reply], Awaitable[None]]] = {
            "!sr": self._sr,
            "!song": self._song,
            "!queue": self._queue,
            "!wrongsong": self._wrongsong,
            "!skip": self._skip,
            "!lurk": self._lurk,
        }

    # --- entry points -------------------------------------------------------
    async def handle(self, line: ChatLine, reply: Reply | None = None) -> None:
        """Every chat message comes through here. Most are ignored."""
        if self._ignored(line):
            return
        words = line.text.split(maxsplit=1)
        if not words or not words[0].startswith("!"):
            return
        command = self._commands.get(words[0].lower())
        if command is None:
            return  # someone else's command (!discord, a Nightbot one, ...)
        args = words[1] if len(words) > 1 else ""
        if reply is None:
            reply = self._threaded(line.message_id)
        await command(line, args, reply)

    async def handle_delete(self, message_id: str) -> None:
        """A mod deleted a message. If it was an `!sr`, the song goes too --
        which is what mods will assume happens."""
        removed = await self._songs.remove_by_message(message_id)
        if removed:
            print(f"[chat] mod deleted the !sr for {removed.title!r}; removed it")

    def _ignored(self, line: ChatLine) -> bool:
        if line.source_channel_id is not None:
            return True
        if line.user_id == self._bot_user_id():
            return True
        denylist = {name.lower() for name in self._get_config().chat.bot_denylist}
        return line.login.lower() in denylist

    def _threaded(self, message_id: str) -> Reply:
        async def reply(text: str) -> None:
            sent, reason = await self._send(text, message_id)
            if not sent:
                # Logged, never retried. A dropped reply is cosmetic: the
                # command itself (say, queueing a song) has already happened,
                # which is the order that matters when 40 people `!sr` at once
                # and the bot's 20-per-30s limit kicks in.
                print(f"[chat] reply not sent ({reason}): {text!r}")

        return reply

    # --- permissions ----------------------------------------------------------
    def _may_request(self, line: ChatLine) -> str | None:
        """None if this viewer may `!sr`, else why not."""
        who = self._get_config().songs.who
        if who == "subs" and not (line.badges & SUB_BADGES):
            return "song requests are for subscribers right now"
        if who == "vip_mod" and not (line.badges & VIP_BADGES):
            return "song requests are for VIPs and mods right now"
        return None

    # --- the commands -------------------------------------------------------
    async def _sr(self, line: ChatLine, args: str, reply: Reply) -> None:
        if not args.strip():
            await reply("usage: !sr <youtube link or song name>")
            return
        if reason := self._may_request(line):
            await reply(reason)
            return
        req, reason = await self._songs.request(
            args, line.name, line.user_id, message_id=line.message_id
        )
        if req is None:
            await reply(reason or "couldn't queue that")
            return
        pos = self._songs.position(req.request_id)
        where = "playing now" if pos == 0 else f"#{pos} in the queue"
        # A search is the bot's guess, so show what it picked: the viewer can
        # check the link and `!wrongsong` it. A link they pasted needs no echo.
        link = f" youtu.be/{req.video_id}" if parse_video_id(args) is None else ""
        await reply(f"queued {_short(req.title)} ({where}){link}")

    async def _song(self, line: ChatLine, args: str, reply: Reply) -> None:
        now = self._songs.now
        if now is None:
            await reply("nothing playing right now")
            return
        paused = " (paused)" if self._songs.paused else ""
        await reply(
            f"now playing{paused}: {_short(now.title)}, requested by {now.user}"
            f" -- youtu.be/{now.video_id}"
        )

    async def _queue(self, line: ChatLine, args: str, reply: Reply) -> None:
        waiting = self._songs.queue
        if not waiting:
            await reply("the queue is empty. !sr <youtube link or song name> to add one")
            return
        nxt = " | ".join(f"{i}. {_short(r.title)}" for i, r in enumerate(waiting[:3], 1))
        more = f" (+{len(waiting) - 3} more)" if len(waiting) > 3 else ""
        mine = [self._songs.position(r.request_id) for r in self._songs.mine(line.user_id)]
        yours = ""
        if mine and mine[0]:
            yours = f" -- yours is #{mine[0]}"
        elif mine:
            yours = " -- yours is playing now"
        await reply(f"up next: {nxt}{more}{yours}")

    async def _wrongsong(self, line: ChatLine, args: str, reply: Reply) -> None:
        mine = self._songs.mine(line.user_id)
        if not mine:
            await reply("you don't have a song in the queue")
            return
        # "Most recent" by when it was asked for -- the one you most likely
        # just fat-fingered.
        latest = max(mine, key=lambda r: r.requested_at)
        await self._songs.remove(latest.request_id)
        await reply(f"removed {_short(latest.title)}")

    async def _skip(self, line: ChatLine, args: str, reply: Reply) -> None:
        # A non-mod's !skip is ignored outright, not answered: a reply would
        # just invite everyone to try it.
        if not line.badges & MOD_BADGES:
            return
        skipped = self._songs.now
        if skipped is None:
            await reply("nothing to skip")
            return
        nxt = await self._songs.skip()
        then = f", up next: {_short(nxt.title)}" if nxt else ""
        await reply(f"skipped {_short(skipped.title)}{then}")

    async def _lurk(self, line: ChatLine, args: str, reply: Reply) -> None:
        last = self._lurked.get(line.user_id, 0.0)
        if time.monotonic() - last < LURK_COOLDOWN_S:
            return
        self._lurked[line.user_id] = time.monotonic()
        await reply(f"enjoy the lurk, {line.name}!")
        if not LURK_GIF.is_file():
            print(f"[chat] !lurk: no gif at {LURK_GIF}")
            return
        # Same message a media redeem sends, so it queues behind any clip
        # instead of cutting one off.
        await self._broadcast(
            {
                "type": "media",
                "src": "/media/gifs/lurky.gif",
                "title": "lurk",
                "user": line.name,
                "interrupt": False,
            }
        )
