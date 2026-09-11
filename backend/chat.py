import os
import time
from collections import deque
from pathlib import Path

from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticationStorageHelper
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope

# The Constants, in SCREAMING_CASE
BOT_SCOPES = [AuthScope.USER_WRITE_CHAT]

BOT_TOKEN_FILE = Path(__file__).with_name(".chat_tokens.json")

# Means "20 messages per 30 seconds" 
RATE_LIMIT = 20
RATE_WINDOW = 30.0

class ChatBot:

    def __init__(self):
        self._twitch: Twitch | None = None
        self._bot_user_id: str | None = None
        self._channel_user_id: str | None = None
        self._time_stamps = deque()

    @property
    def ready(self) -> bool: 
        return self._twitch is not None
