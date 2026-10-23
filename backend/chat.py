"""The bot posts to chat (Phase 12).

A second Twitch identity. `twitch.py` holds the *broadcaster's* token and only
reads (follows, subs, redeems); this module holds the *bot's* token and only
sends. They are separate `Twitch` instances with separate token caches on
purpose -- authorizing the broadcaster token as the bot silently breaks every
EventSub subscription, so the two must never share a file.

Sending is a plain Helix call, so there is no IRC connection and no extra
websocket here. Like the other integrations, a missing credential or failed
auth disables the bot and leaves the rest of the backend running.
"""

import os
import time
from collections import deque
from pathlib import Path

from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope

# A *user* token sending as itself needs only this. user:bot / channel:bot are
# for sending with an app token, which we don't do.
BOT_SCOPES = [AuthScope.USER_WRITE_CHAT]

# The bot's cached user + refresh token. Gitignored, as sensitive as a password.
# Authorized as the wrong account? Delete it and restart to authorize again.
BOT_TOKEN_FILE = Path(__file__).with_name(".chat_tokens.json")

# Means "20 messages per 30 seconds" -- Twitch's limit for a normal account.
# Tripping it gets the bot timed out, not just a message dropped.
RATE_LIMIT = 20
RATE_WINDOW = 30.0


class ChatBot:

    def __init__(self):
        self._twitch: Twitch | None = None
        self._bot_user_id: str | None = None
        self._bot_login: str | None = None
        self._channel_user_id: str | None = None
        # monotonic() times of recent sends, oldest on the left.
        self._time_stamps: deque[float] = deque()

    @property
    def ready(self) -> bool:
        return self._twitch is not None and self._channel_user_id is not None

    @property
    def bot_user_id(self) -> str | None:
        """For loop prevention once chat is read: never answer ourselves."""
        return self._bot_user_id

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> bool:
        """Authenticate as the bot and resolve both ids. False if not configured.

        Reads .env at call time, like TwitchAlerts, because main.py loads it
        after importing this module.
        """
        client_id = os.getenv("TWITCH_CLIENT_ID")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        channel = os.getenv("TWITCH_CHANNEL")

        if not (client_id and client_secret and channel):
            print("[chat] Twitch credentials not set in backend/.env — bot disabled.")
            return False

        self._twitch = await Twitch(client_id, client_secret)

        # First run opens a browser tab. The helper forces Twitch's consent
        # page, whose "Not you?" link is how you switch to the bot account --
        # the browser is normally logged in as the broadcaster.
        helper = UserAuthenticationStorageHelper(
            self._twitch, BOT_SCOPES, storage_path=BOT_TOKEN_FILE
        )
        await helper.bind()

        # No arguments = whoever the token belongs to, i.e. the bot.
        bot = await first(self._twitch.get_users())
        target = await first(self._twitch.get_users(logins=[channel]))
        if bot is None or target is None:
            print(f"[chat] Couldn't resolve the bot or channel {channel!r} — bot disabled.")
            await self.stop()
            return False

        self._bot_user_id = bot.id
        self._bot_login = bot.login
        self._channel_user_id = target.id
        if bot.id == target.id:
            # Works, but it means the bot token was authorized as you.
            print(
                f"[chat] Warning: bot token is authorized as the channel owner "
                f"({bot.login}). Delete {BOT_TOKEN_FILE.name} to re-authorize as the bot."
            )
        print(f"[chat] ready as {bot.login}, posting to {channel}")
        return True

    async def stop(self) -> None:
        if self._twitch is not None:
            await self._twitch.close()
            self._twitch = None
        self._channel_user_id = None

    # --- sending ------------------------------------------------------------
    def _rate_limited(self) -> bool:
        """Forget sends older than the window; full if RATE_LIMIT remain."""
        cutoff = time.monotonic() - RATE_WINDOW
        while self._time_stamps and self._time_stamps[0] < cutoff:
            self._time_stamps.popleft()
        return len(self._time_stamps) >= RATE_LIMIT

    async def send(self, text: str) -> tuple[bool, str | None]:
        """Post `text` as the bot. Returns (sent, reason it wasn't).

        Over the limit, the message is dropped rather than queued: a greeting
        that posts four minutes late is worse than none at all.
        """
        if not self.ready:
            return False, "bot not connected"
        if self._rate_limited():
            return False, "rate limited"

        assert self._twitch and self._channel_user_id and self._bot_user_id
        # Counted on attempt, not success: Twitch counts what we send it, even
        # what it then refuses.
        self._time_stamps.append(time.monotonic())
        try:
            res = await self._twitch.send_chat_message(
                self._channel_user_id, self._bot_user_id, text
            )
        except Exception as exc:
            return False, f"send failed: {exc}"

        # Twitch can accept the request yet refuse the message (followers-only,
        # a blocked term, ...). That arrives as is_sent=False, not an error.
        if not res.is_sent:
            reason = res.drop_reason.message if res.drop_reason else "dropped by Twitch"
            return False, reason
        return True, None
