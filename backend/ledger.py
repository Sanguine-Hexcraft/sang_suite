"""Monsters & Memories Ledger ingester.

mnm.py reads the Ledger for one thing, deaths. This reads all of it -- loot,
coin, harvests, trades, level-ups, party comings and goings -- and keeps every
event in SQLite (backend/ledger.db, gitignored), so stats and recaps become
queries, and the history outlives whatever the game prunes from
Ledger/Archive/.

What the files hold (see mnm.py for the layout and the basic `fNN` keys):

    f01  action code, "act_NN" -- KINDS below names the ones we know
    f02  a ref ("ref_12345"), a plain name, or "name_" + base64 of one
    f03  a JSON *string* of `dNN` fields. Any string in it may or may not be
         base64 -- the same field varies from event to event -- so every one
         is try-decoded. Trades are the exception: readable keys, no base64.
    f07  who did it (you, or a party member in the Social file)
    f09  whose Ledger it is

Two traps that shape the schema:

  * Your own loot, coin and level-ups are written twice: once to the
    Character file and again, a second or so later, to the Social file. The
    Social copy is kept but flagged `mirror`, and stats should filter on
    mirror = 0 (the `ledger` view does), or every kill counts double.
  * Files overlap -- an archived file repeats events that are still in a
    day file -- and the game rewrites each file whole while you play. So rows
    are keyed by a hash of the event itself, and re-reading a file is just an
    INSERT OR IGNORE of everything in it.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from mnm import Broadcast, _parse_time, find_settings_dir

POLL_SECONDS = 2.0
DB_PATH = Path(__file__).with_name("ledger.db")

KINDS = {
    "act_01": "level_up",
    "act_02": "death",
    "act_11": "drop",
    # Unconfirmed: seen right next to drops and destroys of the same item.
    "act_12": "pickup",
    "act_13": "loot",
    # Coins looted from a corpse -- not a kill. A kill with no coin on the body
    # leaves no event at all, so counting these undercounts kills.
    "act_14": "coin_loot",
    "act_15": "destroy",
    "act_16": "trade_give",
    "act_20": "trade_receive",
    # Probably a vendor sale; the payload has an item and a price.
    "act_24": "vendor_sale",
    "act_27": "harvest",
    "act_31": "party_create",
    "act_32": "party_join",
    "act_33": "party_disband",
    "act_34": "party_leave",
    "act_35": "party_leader",
}
# Kinds worth a live message, when they're your own. Everything is stored, but
# loot and harvests land every few seconds and would bury the activity feed.
ANNOUNCE = {"level_up"}
# The announced kinds that go on stream: sent as `type: "alert"` with the kind
# as the alert kind, so each also needs an entry in main.py's DEFAULT_KINDS
# *and* config.json. The rest go out as `type: "ledger"`, which only the
# activity feed shows.
ALERTS = {"level_up"}

# What the Character file also records -- the Social copies of these are the
# mirrors. Party events live only in Social, so those are never mirrors.
_MIRRORED = {"level_up", "loot", "coin_loot"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       TEXT PRIMARY KEY,  -- hash of the raw event, see _event_id
    at       TEXT NOT NULL,     -- UTC ISO, for sorting and ranges
    server   TEXT NOT NULL,     -- "estaire", "beta1"
    owner    TEXT NOT NULL,     -- whose Ledger
    actor    TEXT,              -- who did it
    source   TEXT NOT NULL,     -- "character" | "social"
    mirror   INTEGER NOT NULL,  -- 1 = Social copy of a Character event
    code     TEXT NOT NULL,     -- raw f01
    kind     TEXT NOT NULL,     -- KINDS name, or the raw code if unknown
    zone     TEXT,              -- internal name, e.g. "blindmidden"
    level    INTEGER,
    mob      TEXT,
    item     TEXT,
    qty      INTEGER,
    copper   INTEGER,           -- coin gained or traded, in copper (10:1)
    partner  TEXT,              -- trade partner, or the party's leader
    detail   TEXT               -- the decoded payload, as JSON
);
CREATE INDEX IF NOT EXISTS events_owner_at ON events (owner, at);
CREATE INDEX IF NOT EXISTS events_kind_at ON events (kind, at);
CREATE VIEW IF NOT EXISTS ledger AS SELECT * FROM events WHERE mirror = 0;
"""

_PRICE = re.compile(r"(\d+) platinum (\d+) gold (\d+) silver (\d+) copper")


def _unb64(value: str) -> str:
    """`value` decoded if it's base64 of readable text, else unchanged.

    "Tin Ore" and "harvest" happen to be invalid base64 and fall straight
    through; the printable check catches plain words that decode by accident.
    """
    if not value:
        return value
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return value
    return text if text.isprintable() else value


def _copper(pp: int = 0, gp: int = 0, sp: int = 0, cp: int = 0) -> int:
    return pp * 1000 + gp * 100 + sp * 10 + cp


def _event_id(server: str, owner: str, source: str, event: dict) -> str:
    raw = "\x1f".join(
        str(event.get(k, "")) for k in ("f01", "f02", "f03", "f04", "f05", "f07")
    )
    return hashlib.sha1(f"{server}\x1f{owner}\x1f{source}\x1f{raw}".encode()).hexdigest()


def _decode(path: Path, event: dict) -> tuple | None:
    """One Ledger event as a row for `events`, or None if it isn't one."""
    if not isinstance(event, dict) or "f01" not in event:
        return None
    try:
        at = _parse_time(event["f04"])
    except (KeyError, ValueError):
        return None
    # .../<server>/<Character>/Ledger/[Archive/]<file>
    ledger_dir = path.parent if path.parent.name == "Ledger" else path.parent.parent
    server = ledger_dir.parent.parent.name
    owner = event.get("f09") or ledger_dir.parent.name
    source = "social" if "_Social_" in path.name else "character"
    code = event["f01"]
    kind = KINDS.get(code, code)

    try:
        payload = json.loads(event.get("f03") or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not kind.startswith("trade_"):
        payload = {k: _unb64(v) if isinstance(v, str) else v for k, v in payload.items()}

    ref = event.get("f02") or ""
    if ref.startswith("name_"):
        ref = _unb64(ref[5:])
    zone = event.get("f05") or ""
    zone = _unb64(zone[5:]) if zone.startswith("zone_") else zone or None

    mob = item = qty = copper = partner = None
    if kind in ("loot", "drop", "pickup", "destroy", "vendor_sale", "harvest"):
        item = payload.get("d04") or None
        qty = payload.get("d01")
        if kind == "loot":
            mob = payload.get("d02") or None
        elif kind == "vendor_sale" and (m := _PRICE.search(payload.get("d03", ""))):
            copper = _copper(*map(int, m.groups()))
    elif kind == "coin_loot":
        mob = payload.get("d13") or None
        copper = payload.get("d11")
    elif kind.startswith("trade_"):
        partner = ref or None
        copper = _copper(*(int(payload.get(f"trading{c}", 0)) for c in ("Pp", "Gp", "Sp", "Cp")))
        items = payload.get("items") or []
        qty = sum(int(i.get("quantity", 0)) for i in items)
        item = ", ".join(i.get("name", "?") for i in items) or None
    elif kind.startswith("party_"):
        partner = ref or None

    actor = event.get("f07") or owner
    mirror = int(source == "social" and actor == owner and kind in _MIRRORED)
    return (
        _event_id(server, owner, source, event),
        at.astimezone(timezone.utc).isoformat(),
        server, owner, actor, source, mirror, code, kind, zone,
        event.get("f06"), mob, item, qty, copper, partner,
        json.dumps(payload, ensure_ascii=False),
    )


COLUMNS = (
    "id", "at", "server", "owner", "actor", "source", "mirror", "code", "kind", "zone",
    "level", "mob", "item", "qty", "copper", "partner", "detail",
)
INSERT = f"INSERT OR IGNORE INTO events VALUES ({', '.join('?' * len(COLUMNS))})"


def _new_level(row: dict) -> int | None:
    return json.loads(row["detail"]).get("d21", row["level"])


def _describe(row: dict) -> str:
    if row["kind"] == "level_up":
        return f"{row['actor']} reached level {_new_level(row)}"
    return f"{row['actor']}: {row['kind']}"


class LedgerIngester:
    """Polls every Ledger file and copies new events into SQLite.

    Owns the database: one connection, guarded by a lock, because polls run in
    a worker thread while API reads come from the event loop. New events of an
    ANNOUNCE kind are broadcast as `type: "ledger"` messages.
    """

    def __init__(self, broadcast: Broadcast, db_path: Path = DB_PATH):
        self._broadcast = broadcast
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._settings_dir: Path | None = None
        # path -> mtime it was last ingested at, so a quiet poll reads nothing.
        self._mtimes: dict[Path, float] = {}
        # When the first scan finished. Rows older than this predate the
        # watcher and are never announced -- see _announce.
        self._watch_from: datetime | None = None

    # --- ingesting ------------------------------------------------------------
    def _scan(self) -> list[dict]:
        """Ingest every Ledger file that changed. Returns the rows that were new."""
        # Re-discovered each pass for the same reason as DeathWatcher: an update
        # can leave a dead prefix behind that still "exists".
        settings_dir = find_settings_dir()
        if settings_dir is None:
            return []
        if settings_dir != self._settings_dir:
            print(f"[ledger] Watching {settings_dir}")
            self._settings_dir = settings_dir
            self._mtimes.clear()
        added: list[dict] = []
        for path in settings_dir.glob("*/*/Ledger/**/*.json"):
            try:
                mtime = path.stat().st_mtime
                if self._mtimes.get(path) == mtime:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Gone, or caught mid-rewrite: leave the mtime unrecorded so the
                # next poll tries again.
                continue
            events = data.get("c01", []) if isinstance(data, dict) else []
            rows = [r for r in (_decode(path, e) for e in events) if r is not None]
            # One execute per row rather than executemany, because rowcount is
            # the only way INSERT OR IGNORE says which rows it actually kept.
            with self._lock, self._db:
                for row in rows:
                    if self._db.execute(INSERT, row).rowcount:
                        added.append(dict(zip(COLUMNS, row)))
            self._mtimes[path] = mtime
        return added

    async def _run(self) -> None:
        while True:
            try:
                added = await asyncio.to_thread(self._scan)
                if self._watch_from is None:
                    # The first pass is the backfill: everything already on
                    # disk, none of it news. A restart must not replay it.
                    self._watch_from = datetime.now(timezone.utc)
                    print(f"[ledger] Ingested {len(added)} existing events")
                elif added:
                    await self._announce(added)
            except Exception as exc:  # never let one bad pass kill the loop
                print(f"[ledger] Poll failed: {exc}")
            await asyncio.sleep(POLL_SECONDS)

    async def _announce(self, rows: list[dict]) -> None:
        for row in sorted(rows, key=lambda r: r["at"]):
            # Mirrors are the Social copy of a Character event, already
            # announced (or about to be) from the Character file. The time check
            # covers a file caught mid-write by the first pass: its rows arrive
            # a poll late, but they predate the watcher, so they're history.
            if (
                row["kind"] not in ANNOUNCE
                or row["mirror"]
                or row["actor"] != row["owner"]
                or datetime.fromisoformat(row["at"]) <= self._watch_from
            ):
                continue
            message = {k: row[k] for k in COLUMNS if k not in ("id", "at", "detail")}
            # The relay stamps its own `at` over a message's, so the Ledger
            # time travels as happened_at -- as died_at does for deaths.
            message |= {"happened_at": row["at"], "text": _describe(row)}
            if row["kind"] in ALERTS:
                # The alert shape twitch.py sends: user, and amount -- here the
                # new level.
                message |= {"type": "alert", "user": row["actor"], "amount": _new_level(row)}
            else:
                message["type"] = "ledger"
            await self._broadcast(message)

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
        self._db.close()

    # --- querying -------------------------------------------------------------
    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args)]

    def summary(self, since: datetime, owner: str | None = None) -> dict:
        """What happened from `since` on: the raw material for a stream recap.

        `owner` narrows it to one character's Ledger; party members' events
        still count under the Ledger that saw them (see `party`).
        """
        where = "at >= ?" + (" AND owner = ?" if owner else "")
        args = (since.astimezone(timezone.utc).isoformat(),) + ((owner,) if owner else ())
        # Yours alone: party members' events don't count toward your totals.
        mine = f"{where} AND actor = owner"

        def q(sql: str) -> list[dict]:
            return self._query(sql, args)

        return {
            "since": args[0],
            "counts": {r["kind"]: r["n"] for r in q(
                f"SELECT kind, COUNT(*) AS n FROM ledger WHERE {mine} GROUP BY kind")},
            "copper": {r["kind"]: r["c"] for r in q(
                f"SELECT kind, SUM(copper) AS c FROM ledger WHERE {mine} AND copper > 0 "
                "AND kind IN ('coin_loot', 'vendor_sale') GROUP BY kind")},
            "top_mobs": q(
                f"SELECT mob, COUNT(*) AS n FROM ledger WHERE {mine} AND kind = 'coin_loot' "
                "AND mob IS NOT NULL GROUP BY mob ORDER BY n DESC LIMIT 5"),
            "top_loot": q(
                f"SELECT item, SUM(COALESCE(qty, 1)) AS n FROM ledger WHERE {mine} "
                "AND kind = 'loot' GROUP BY item ORDER BY n DESC LIMIT 5"),
            "harvested": q(
                f"SELECT item, SUM(COALESCE(qty, 1)) AS n FROM ledger WHERE {mine} "
                "AND kind = 'harvest' GROUP BY item ORDER BY n DESC LIMIT 5"),
            # Zones in the order first visited this session.
            "zones": [r["zone"] for r in q(
                f"SELECT zone, MIN(at) AS first FROM ledger WHERE {mine} AND zone IS NOT NULL "
                "GROUP BY zone ORDER BY first")],
            "levels": q(
                f"SELECT at, level, json_extract(detail, '$.d21') AS new_level FROM ledger "
                f"WHERE {mine} AND kind = 'level_up' ORDER BY at"),
            # Everyone you shared loot or a party with, most-seen first.
            "party": q(
                f"SELECT actor AS name, COUNT(*) AS n FROM ledger WHERE {where} "
                "AND actor != owner GROUP BY actor ORDER BY n DESC LIMIT 10"),
        }

    def recent(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM ledger" + (" WHERE kind = ?" if kind else "")
        return self._query(sql + " ORDER BY at DESC LIMIT ?", ((kind,) if kind else ()) + (limit,))
