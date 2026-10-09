"""Monsters & Memories death counter (Phase 14).

M&M has no EverQuest-style chat log, but it does keep a per-character event
journal it calls the Ledger -- JSON files under

    <settings root>/<server>/<Character>/Ledger/<Char>_Character_YYYY-MM-DD.json

Each file is `{"c01": [event, ...], ...}` and every event is a dict of opaque
`fNN` keys. The ones that matter here:

    f01  action code; "act_02" is a death (empty payload, recorded in the zone
         you died in -- the next event is often in your bind zone)
    f04  ISO timestamp with a local offset and 7 fractional digits
    f05  "zone_" + base64 of the zone's internal name, e.g. "blindmidden"
    f06  level at the time
    f07  character name

The game rewrites the whole file while you play rather than appending to it,
so a read can land mid-write; a file that fails to parse is simply retried on
the next poll.

Like twitch.py this knows nothing about FastAPI: main.py injects `broadcast`.
"""

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

Broadcast = Callable[[dict], Awaitable[None]]

DEATH_ACTION = "act_02"
POLL_SECONDS = 2.0
# A saved session older than this at boot is yesterday's stream, not this one.
# Restarting the backend mid-stream stays well inside it and keeps the count.
SESSION_MAX_AGE = timedelta(hours=6)

# Gitignored, next to this file: when the current stream started, plus any
# manual correction. The count itself is never stored -- it is recomputed from
# the Ledger, which is what makes a restart unable to lose or double it.
STATE_PATH = Path(__file__).with_name("deaths.json")

# The umu launcher keeps one Wine prefix per install dir, and an update that
# renames the dir leaves the old prefix behind (mnm_fnf -> mnm in Aug 2026).
# `steamuser` only: users/<you> is a symlink to it, and globbing both would
# count every death twice.
_PREFIX_GLOB = "*/pfx/drive_c/users/steamuser/AppData/LocalLow/Niche Worlds Cult/Monsters and Memories"


def find_settings_dir() -> Path | None:
    """The M&M settings folder the game is actually using, or None.

    MNM_SETTINGS_DIR (from backend/.env) wins. Otherwise pick the most recently
    written candidate, the same rule mnm-tools uses, so a dead prefix left over
    from an update never shadows the live one.
    """
    override = os.getenv("MNM_SETTINGS_DIR")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    root = Path.home() / ".local/share/mnm"
    candidates = [p for p in root.glob(_PREFIX_GLOB) if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=_last_written)


def _last_written(settings_dir: Path) -> float:
    times = [f.stat().st_mtime for f in (settings_dir / "Player.log", settings_dir / "settings.json") if f.exists()]
    return max(times, default=settings_dir.stat().st_mtime)


def _parse_time(stamp: str) -> datetime:
    # Python 3.11+ accepts the 7th fractional digit; older versions don't, and
    # trimming to microseconds costs nothing.
    head, dot, rest = stamp.partition(".")
    if dot:
        digits = "".join(c for c in rest if c.isdigit())
        rest = digits[:6] + rest[len(digits):]
        stamp = f"{head}.{rest}"
    at = datetime.fromisoformat(stamp)
    # Some files (the beta's, at least) write no offset at all. The game stamps
    # local time either way, and astimezone() reads a naive value as local --
    # which matters because naive and aware datetimes can't be compared.
    return at if at.tzinfo else at.astimezone()


def _zone_name(raw: str) -> str:
    if not raw.startswith("zone_"):
        return raw or "Unknown"
    try:
        name = base64.b64decode(raw[5:]).decode()
    except ValueError:
        return "Unknown"
    # Internal names are lowercase single words ("blindmidden"); multi-word ones
    # may use underscores. Good enough for an overlay caption.
    return name.replace("_", " ").title()


@dataclass(frozen=True)
class Death:
    at: datetime
    character: str
    server: str
    zone: str
    level: int | None

    def as_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "character": self.character,
            "server": self.server,
            "zone": self.zone,
            "level": self.level,
        }


def _deaths_in(path: Path) -> list[Death]:
    """Every death recorded in one Ledger file. Raises on a half-written file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    # .../<server>/<Character>/Ledger/<file>
    server = path.parent.parent.parent.name
    deaths = []
    for event in data.get("c01", []):
        if event.get("f01") != DEATH_ACTION:
            continue
        try:
            at = _parse_time(event["f04"])
        except (KeyError, ValueError):
            continue
        deaths.append(
            Death(
                at=at,
                character=event.get("f07") or path.parent.parent.name,
                server=server,
                zone=_zone_name(event.get("f05", "")),
                level=event.get("f06"),
            )
        )
    return deaths


class DeathWatcher:
    """Polls the Ledger and broadcasts each new death of the current stream."""

    def __init__(self, broadcast: Broadcast):
        self._broadcast = broadcast
        self._task: asyncio.Task | None = None
        self._settings_dir: Path | None = None
        # path -> (mtime it was parsed at, deaths in it). Every death is kept,
        # not just this stream's, so a reset is a re-filter, not a re-read.
        self._files: dict[Path, tuple[float, list[Death]]] = {}
        # Deaths already announced, so a file rewrite doesn't re-alert them.
        self._announced: set[Death] = set()
        self.session_start, self.adjust = self._load_state()

    # --- state ----------------------------------------------------------------
    def _load_state(self) -> tuple[datetime, int]:
        now = datetime.now(timezone.utc)
        try:
            saved = json.loads(STATE_PATH.read_text())
            start = datetime.fromisoformat(saved["session_start"])
            if now - start < SESSION_MAX_AGE:
                return start, int(saved.get("adjust", 0))
            print("[deaths] Last session is stale; starting a new stream count")
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, OSError) as exc:
            print(f"[deaths] {STATE_PATH.name} unusable, starting fresh: {exc}")
        self._save_state(now, 0)
        return now, 0

    def _save_state(self, start: datetime, adjust: int) -> None:
        try:
            STATE_PATH.write_text(
                json.dumps({"session_start": start.isoformat(), "adjust": adjust}, indent=2) + "\n"
            )
        except OSError as exc:
            print(f"[deaths] Couldn't write {STATE_PATH.name}: {exc}")

    # --- counting -------------------------------------------------------------
    def session_deaths(self) -> list[Death]:
        deaths = {d for _, ds in self._files.values() for d in ds if d.at >= self.session_start}
        return sorted(deaths, key=lambda d: d.at)

    def snapshot(self) -> dict:
        deaths = self.session_deaths()
        return {
            # adjust can't drag the total below zero, whatever was clicked.
            "count": max(0, len(deaths) + self.adjust),
            "session_start": self.session_start.isoformat(),
            "last": deaths[-1].as_dict() if deaths else None,
            "watching": str(self._settings_dir) if self._settings_dir else None,
        }

    def reset(self) -> dict:
        """Start a new stream: count from now, forget manual corrections."""
        self.session_start = datetime.now(timezone.utc)
        self.adjust = 0
        self._save_state(self.session_start, self.adjust)
        return self.snapshot()

    def nudge(self, delta: int) -> dict:
        """Manual correction, e.g. -1 for a false positive."""
        raw = len(self.session_deaths())
        self.adjust = max(self.adjust + delta, -raw)
        self._save_state(self.session_start, self.adjust)
        return self.snapshot()

    # --- polling --------------------------------------------------------------
    def _scan(self) -> None:
        """Re-parse any Ledger file that changed since the last pass."""
        if self._settings_dir is None or not self._settings_dir.is_dir():
            self._settings_dir = find_settings_dir()
            if self._settings_dir is None:
                return
            print(f"[deaths] Watching {self._settings_dir}")
        for path in self._settings_dir.glob("*/*/Ledger/*_Character_*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cached = self._files.get(path)
            if cached and cached[0] == mtime:
                continue
            try:
                self._files[path] = (mtime, _deaths_in(path))
            except (ValueError, OSError):
                # Most likely caught the game mid-rewrite. Leave the old entry
                # (if any) in place and try again next poll.
                continue

    async def _run(self) -> None:
        # The first pass only learns what's already there: a restart mid-stream
        # must not replay every death so far as fresh alerts.
        await asyncio.to_thread(self._scan)
        self._announced.update(self.session_deaths())
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await asyncio.to_thread(self._scan)
                await self._announce_new()
            except Exception as exc:  # never let one bad pass kill the loop
                print(f"[deaths] Poll failed: {exc}")

    async def _announce_new(self) -> None:
        deaths = self.session_deaths()
        fresh = [d for d in deaths if d not in self._announced]
        if not fresh:
            return
        # Count each one in turn, so two deaths in one poll tick up 1, 2.
        before = len(deaths) - len(fresh)
        for i, death in enumerate(fresh, start=1):
            self._announced.add(death)
            await self._broadcast(
                {
                    "type": "death",
                    "count": max(0, before + i + self.adjust),
                    **death.as_dict(),
                    "text": f"{death.character} died in {death.zone}",
                }
            )

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
