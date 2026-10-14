"""Monsters & Memories zone banner: follows Player.log for zone changes.

The Ledger only shows a zone once something happens in it. Unity's Player.log
says so the moment you zone, in a fixed sequence of lines per load:

    [Client][ZONE] Start zoning process to nightharbore     <- where to
    ... loading ...
    [Client][ZONE] Done waiting, hide LoadingPanel          <- you can see it

The banner goes out on the second line, not the first: announcing on "Start"
would play it behind the loading screen.

The game moves Player.log to Player-prev.log on every launch and starts a new
one, so a log that shrinks or changes inode is read again from the top.

Display names and lore lines live in zones.json next to this file, re-read on
every zone change so an edit shows up on the next zone without a restart.
"""

import asyncio
import json
import re
from pathlib import Path

from mnm import Broadcast, find_settings_dir

POLL_SECONDS = 1.0
ZONES_PATH = Path(__file__).with_name("zones.json")

_START = re.compile(r"\[Client\]\[ZONE\] Start zoning process to (\S+)")
_SHOWN = "[Client][ZONE] Done waiting, hide LoadingPanel"


def zone_info(zone: str) -> dict:
    """`zone`'s display name and lore line from zones.json, or a best guess."""
    try:
        entry = json.loads(ZONES_PATH.read_text(encoding="utf-8")).get(zone) or {}
    except (OSError, ValueError) as exc:
        print(f"[zones] {ZONES_PATH.name} unusable: {exc}")
        entry = {}
    return {
        "zone": zone,
        # Internal names are lowercase run-together words; title-casing is a
        # poor guess ("Nightharbore"), which is why zones.json exists.
        "name": entry.get("name") or zone.replace("_", " ").title(),
        "lore": entry.get("lore") or "",
    }


class ZoneWatcher:
    """Tails Player.log and broadcasts a `zone` message on every zone-in."""

    def __init__(self, broadcast: Broadcast):
        self._broadcast = broadcast
        self._task: asyncio.Task | None = None
        self._log: Path | None = None
        # Where we are in the log: (inode, byte offset), plus any trailing
        # partial line, held until the game finishes writing it.
        self._inode: int | None = None
        self._offset = 0
        self._partial = b""
        # Zone named by "Start zoning", waiting for the loading screen to go.
        self._pending: str | None = None
        self.current: str | None = None

    def snapshot(self) -> dict:
        return {
            **(zone_info(self.current) if self.current else {"zone": None, "name": None, "lore": ""}),
            "watching": str(self._log) if self._log else None,
        }

    # --- reading ----------------------------------------------------------------
    def _read_new(self) -> list[str]:
        """Complete lines appended since the last call."""
        settings_dir = find_settings_dir()
        if settings_dir is None:
            return []
        log = settings_dir / "Player.log"
        if log != self._log:
            print(f"[zones] Watching {log}")
            self._log, self._inode = log, None
        try:
            stat = log.stat()
            if stat.st_ino != self._inode or stat.st_size < self._offset:
                # A new log (game relaunched, or first look): from the top.
                self._inode, self._offset, self._partial = stat.st_ino, 0, b""
            if stat.st_size == self._offset:
                return []
            with log.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return []
        self._offset += len(chunk)
        *lines, self._partial = (self._partial + chunk).split(b"\n")
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]

    def _zone_ins(self, lines: list[str]) -> list[str]:
        """The zones these lines finish loading into, in order."""
        entered = []
        for line in lines:
            if m := _START.search(line):
                self._pending = m.group(1)
            elif _SHOWN in line and self._pending:
                entered.append(self._pending)
                self.current, self._pending = self._pending, None
        return entered

    async def _run(self) -> None:
        # The first read of the log only learns where we are now: a restart
        # mustn't replay a day of zoning. "Read", not "pass": until a log turns
        # up, passes read nothing, and the first one that does is still history.
        primed = False
        while True:
            try:
                entered = self._zone_ins(await asyncio.to_thread(self._read_new))
                if primed:
                    for zone in entered:
                        info = zone_info(zone)
                        await self._broadcast(
                            {"type": "zone", **info, "text": f"Entered {info['name']}"}
                        )
                primed = self._inode is not None
            except Exception as exc:  # never let one bad pass kill the loop
                print(f"[zones] Poll failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)

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
