"""Twitch EventSub -> alert relay (Phase 7).

This is the *third* websocket in the project, and like OBSController it's an
outbound one: the backend is a client dialing OUT to Twitch. Twitch pushes an
event (someone followed, subscribed, cheered, raided), we translate it into the
same alert dict the control panel already sends, and hand it to the /ws relay
so every connected overlay sees it.

Nothing here talks to the browser directly — `broadcast` is injected by main.py
so this module stays independent of FastAPI.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.object.eventsub import (
    ChannelCheerEvent,
    ChannelFollowEvent,
    ChannelPointsCustomRewardRedemptionAddEvent,
    ChannelRaidEvent,
    ChannelSubscribeEvent,
    ChannelSubscriptionMessageEvent,
)
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope

if TYPE_CHECKING:  # avoids a circular import; main.py imports this module
    from main import RewardConfig

# Raids need no scope; the other three each need the broadcaster's permission.
# Every change to this list invalidates the cached token and forces a browser
# re-authorize, so USER_READ_CHAT is added now despite being unused until the
# loot box lands -- one re-auth instead of two.
SCOPES = [
    AuthScope.MODERATOR_READ_FOLLOWERS,  # follows
    AuthScope.CHANNEL_READ_SUBSCRIPTIONS,  # subs, resubs
    AuthScope.BITS_READ,  # cheers
    AuthScope.CHANNEL_READ_REDEMPTIONS,  # channel point redeems (Phase 10)
    AuthScope.USER_READ_CHAT,  # not used yet; see comment above
]

# Where the user token + refresh token get cached, so you only authorize once.
# Sits next to this file; gitignored — it is as sensitive as a password.
TOKEN_FILE = Path(__file__).with_name(".twitch_tokens.json")

# The alert payload handed to the overlay. `type` and `text` are what the
# overlay already understands; `kind`, `user` and `amount` are additive, so
# older overlay code keeps working and newer code can style per-event.
AlertBroadcast = Callable[[dict], Awaitable[None]]
# Returns the routing for a reward id, or None when it isn't configured.
RewardLookup = Callable[[str], "RewardConfig | None"]


def _alert(kind: str, user: str, text: str, amount: int | None = None) -> dict:
    return {"type": "alert", "kind": kind, "user": user, "text": text, "amount": amount}


class TwitchAlerts:
    """Owns the Twitch connection and the four EventSub subscriptions.

    Mirrors OBSController's philosophy: if it can't start (no credentials, no
    network), it says so and the rest of the backend carries on running.
    """

    def __init__(self, broadcast: AlertBroadcast, reward_lookup: RewardLookup):
        self._broadcast = broadcast
        self._reward_lookup = reward_lookup
        self._twitch: Twitch | None = None
        self._eventsub: EventSubWebsocket | None = None

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> bool:
        """Authenticate and subscribe. Returns False if not configured.

        Config is read here rather than at import time because main.py loads
        .env *after* importing this module.
        """
        client_id = os.getenv("TWITCH_CLIENT_ID")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        channel = os.getenv("TWITCH_CHANNEL")

        if not (client_id and client_secret and channel):
            print(
                "[twitch] TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET / TWITCH_CHANNEL "
                "not set in backend/.env — Twitch alerts disabled."
            )
            return False

        # Optional overrides pointing at the Twitch CLI mock server:
        #   twitch event websocket start-server -S -p 8081
        # Leave unset to talk to the real Twitch.
        connection_url = os.getenv("TWITCH_EVENTSUB_WS") or None
        subscription_url = os.getenv("TWITCH_EVENTSUB_API") or None

        self._twitch = await Twitch(client_id, client_secret)

        # Loads the cached token if present and still refreshable; otherwise
        # opens a browser tab for the one-time Authorize click, then writes
        # TOKEN_FILE so restarts are silent.
        helper = UserAuthenticationStorageHelper(
            self._twitch, SCOPES, storage_path=TOKEN_FILE
        )
        await helper.bind()

        user = await first(self._twitch.get_users(logins=[channel]))
        if user is None:
            print(f"[twitch] No such channel: {channel!r} — Twitch alerts disabled.")
            return False
        uid = user.id

        self._eventsub = EventSubWebsocket(
            self._twitch,
            connection_url=connection_url,
            subscription_url=subscription_url,
        )
        self._eventsub.start()

        # channel.follow is v2-only and wants a moderator id as well as the
        # broadcaster id. You are your own moderator, hence uid twice.
        follow_sub = await self._eventsub.listen_channel_follow_v2(uid, uid, self._on_follow)
        sub_sub = await self._eventsub.listen_channel_subscribe(uid, self._on_subscribe)
        cheer_sub = await self._eventsub.listen_channel_cheer(uid, self._on_cheer)
        # channel.subscribe fires for NEW subscribers only -- a renewal is a
        # channel.subscription.message, which is why resubs never alerted.
        # Same channel:read:subscriptions scope as channel.subscribe, so adding
        # it needs no re-authorize.
        resub_sub = await self._eventsub.listen_channel_subscription_message(
            uid, self._on_resub
        )
        redeem_sub = await self._eventsub.listen_channel_points_custom_reward_redemption_add(
            uid, self._on_redeem
        )
        # Note the argument order: raid takes the callback FIRST, unlike the
        # three above. Easy to get wrong.
        raid_sub = await self._eventsub.listen_channel_raid(
            self._on_raid, to_broadcaster_user_id=uid
        )

        where = connection_url or "Twitch"
        print(f"[twitch] Listening for {channel} (id {uid}) via {where}")

        # twitchAPI matches each incoming event by the subscription id it got
        # back above. The CLI mock's `trigger` invents a random id unless you
        # pass -u, so a bare trigger is silently dropped. Print ready-to-paste
        # commands (with the right -u and -t) so mock testing just works.
        if connection_url:
            subs = {
                "channel.follow": follow_sub,
                "channel.subscribe": sub_sub,
                "channel.subscription.message": resub_sub,
                "channel.cheer": cheer_sub,
                "channel.channel_points_custom_reward_redemption.add": redeem_sub,
                "channel.raid": raid_sub,
            }
            # -t is the *receiver* id for every one of these (the broadcaster
            # being followed/subbed/cheered, or the channel being raided into),
            # which is you. For a raid the raider is a separate random user the
            # mock fills in. -u forces the subscription id to match.
            print("[twitch] mock test commands:")
            for event, sub_id in subs.items():
                print(
                    f"  twitch event trigger {event} --transport=websocket "
                    f"-t {uid} -u {sub_id}"
                )
        return True

    async def stop(self) -> None:
        if self._eventsub is not None:
            await self._eventsub.stop()
            self._eventsub = None
        if self._twitch is not None:
            await self._twitch.close()
            self._twitch = None

    # --- event handlers -----------------------------------------------------
    # Each one pulls the interesting bits out of the payload, phrases the alert
    # text, and pushes it to every connected overlay.
    async def _on_follow(self, data: ChannelFollowEvent) -> None:
        name = data.event.user_name
        await self._broadcast(_alert("follow", name, f"New Follower: {name}"))

    async def _on_subscribe(self, data: ChannelSubscribeEvent) -> None:
        e = data.event
        # Twitch sends tier as "1000"/"2000"/"3000"; show it as 1/2/3.
        tier = (e.tier or "1000")[0]
        text = f"{e.user_name} subscribed! (Tier {tier})"
        if e.is_gift:
            text = f"{e.user_name} received a gift sub! (Tier {tier})"
        await self._broadcast(_alert("sub", e.user_name, text, int(tier)))

    async def _on_resub(self, data: ChannelSubscriptionMessageEvent) -> None:
        """A renewal. Carries the month count, which is the point of a resub."""
        e = data.event
        tier = (e.tier or "1000")[0]
        months = e.cumulative_months
        # cumulative_months is optional in the payload; without it, say less
        # rather than printing "for None months".
        if months:
            plural = "" if months == 1 else "s"
            text = f"{e.user_name} resubscribed for {months} month{plural}! (Tier {tier})"
        else:
            text = f"{e.user_name} resubscribed! (Tier {tier})"
        # `amount` stays the tier, matching kind="sub" above -- the month count
        # is already in the text and the overlay renders text only.
        await self._broadcast(_alert("resub", e.user_name, text, int(tier)))

    async def _on_cheer(self, data: ChannelCheerEvent) -> None:
        e = data.event
        # Anonymous cheers arrive with user_name set to None.
        name = "Anonymous" if e.is_anonymous else (e.user_name or "Anonymous")
        await self._broadcast(
            _alert("cheer", name, f"{name} cheered {e.bits} bits!", e.bits)
        )

    async def _on_raid(self, data: ChannelRaidEvent) -> None:
        e = data.event
        name = e.from_broadcaster_user_name
        await self._broadcast(
            _alert("raid", name, f"{name} is raiding with {e.viewers}!", e.viewers)
        )

    async def _on_redeem(
        self, data: ChannelPointsCustomRewardRedemptionAddEvent
    ) -> None:
        """A viewer spent channel points.

        Read-only by design: this app can only fulfil or refund redemptions for
        rewards it created itself through the API, and yours live in the Twitch
        dashboard. So react, never manage.
        """
        e = data.event
        reward = e.reward
        routing = self._reward_lookup(reward.id)

        if routing is None:
            # The discovery path. Printed copy-pasteable because looking an id
            # up in the API explorer every time you add a reward is miserable.
            print(
                f"[redeem] unconfigured reward {reward.title!r} "
                f"id={reward.id} cost={reward.cost} -- add it to config.json"
            )
            return

        if "alert" not in routing.actions:
            return

        # A reward's own title is almost always what you want on screen, so
        # `label` only has to be set when you want to override it.
        title = routing.label or reward.title
        text = f"{e.user_name} redeemed {title}!"
        # user_input is "" for rewards that don't prompt for text.
        if e.user_input:
            text = f"{text} ({e.user_input})"
        await self._broadcast(_alert("redeem", e.user_name, text, reward.cost))
