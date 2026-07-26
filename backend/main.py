import json
import os

from contextlib import asynccontextmanager
from pathlib import Path

import simpleobsws
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from twitch import TwitchAlerts


# --- config -----------------------------------------------------------------
def _load_env(path: str = ".env") -> None:
    """Read simple KEY=VALUE lines from a .env file into os.environ.

    Dependency-free so we don't pull in python-dotenv. Existing environment
    variables win (setdefault), so you can still override a value per-shell.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

OBS_WS_URL = os.getenv("OBS_WS_URL", "ws://localhost:4455")
OBS_WS_PASSWORD = os.getenv("OBS_WS_PASSWORD")  # None = connect without auth


# --- widget settings (Phase 8) ----------------------------------------------
# Distinct from .env above: that's secrets and machine wiring, this is the
# stuff you tweak while streaming. Every field has a default, so the app runs
# with no config.json at all and only writes one once you save from /control.
CONFIG_PATH = Path(__file__).parent / "config.json"


class AlertKindConfig(BaseModel):
    label: str
    # Hex only — the overlay splits this into an "r g b" triple to feed
    # rgb(var(--accent) / alpha), and a plain hex is what an <input
    # type="color"> on the control panel produces.
    accent: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    # Names a jingle in the overlay's SOUNDS table (frontend/src/audio/
    # sounds.ts), which owns the list; deliberately not an enum here so adding a
    # tune there doesn't need a matching backend change. An unknown name falls
    # back to the default jingle rather than erroring. Defaulted so a
    # config.json written before this field still validates.
    sound: str = "levelup"


DEFAULT_KINDS = {
    "follow": AlertKindConfig(label="NEW FOLLOWER", accent="#b06bff", sound="follow"),
    "sub": AlertKindConfig(label="NEW SUBSCRIBER", accent="#ffd166", sound="sub"),
    "cheer": AlertKindConfig(label="BITS INCOMING", accent="#4dd6ff", sound="cheer"),
    "raid": AlertKindConfig(label="INCOMING RAID", accent="#ff7a00", sound="raid"),
    # Manual alerts from /control arrive with no `kind` and land here.
    "generic": AlertKindConfig(label="ALERT", accent="#ff2d55", sound="levelup"),
}


class AlertConfig(BaseModel):
    # Bounded so a typo in config.json can't wedge an alert on screen forever.
    duration_ms: int = Field(default=8000, ge=500, le=60_000)
    # Peak gain of the level-up jingle, 0 = muted. The default is tuned for the
    # triangle wave the overlay synthesises; a squarer waveform reads louder at
    # the same gain, so retune this if you change the oscillator.
    volume: float = Field(default=0.45, ge=0.0, le=1.0)
    kinds: dict[str, AlertKindConfig] = DEFAULT_KINDS


class Config(BaseModel):
    alerts: AlertConfig = AlertConfig()


def load_config() -> Config:
    """Read config.json, falling back to defaults if it's missing or broken.

    A malformed file shouldn't stop the server booting mid-stream — same
    graceful-degradation stance as OBS and Twitch being unreachable.
    """
    if CONFIG_PATH.exists():
        try:
            return Config.model_validate_json(CONFIG_PATH.read_text())
        except (ValueError, OSError) as exc:
            print(f"[config] {CONFIG_PATH.name} unusable, using defaults: {exc}")
    return Config()


def save_config(cfg: Config) -> None:
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2) + "\n")


config = load_config()


# --- OBS control ------------------------------------------------------------
class OBSController:
    """Holds one persistent connection to OBS's own websocket server.

    Note this is the *opposite* direction from the /ws relay below: here the
    backend is a client dialing OUT to OBS (default ws://localhost:4455),
    whereas /ws is a server the browser dials IN to.

    Connects lazily and reconnects if the socket has dropped, so the backend
    can start fine even when OBS isn't running yet — the error only surfaces
    when you actually press a control button.
    """

    def __init__(self, url: str, password: str | None):
        self._url = url
        self._password = password
        self._client: simpleobsws.WebSocketClient | None = None

    async def _ready_client(self) -> simpleobsws.WebSocketClient:
        if self._client is not None and self._client.is_identified():
            return self._client
        client = simpleobsws.WebSocketClient(url=self._url, password=self._password)
        await client.connect()
        await client.wait_until_identified()
        self._client = client
        return client

    async def call(self, request: simpleobsws.Request) -> simpleobsws.RequestResponse:
        """Send one request to OBS, translating failures into clean HTTP errors."""
        try:
            client = await self._ready_client()
            resp = await client.call(request)
        except (ConnectionError, OSError):
            # Couldn't reach OBS — drop the dead client so the next call retries.
            self._client = None
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Can't reach OBS at {self._url}. Is OBS running with the "
                    "WebSocket server enabled (Tools → WebSocket Server Settings)?"
                ),
            )
        if not resp.ok():
            raise HTTPException(
                status_code=502,
                detail=f"OBS rejected {request.requestType}: {resp.requestStatus}",
            )
        return resp

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


obs = OBSController(OBS_WS_URL, OBS_WS_PASSWORD)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Try to connect at startup, but don't crash the app if OBS is offline —
    # the connection is lazy, so a button press later will retry.
    try:
        await obs._ready_client()
    except (ConnectionError, OSError):
        pass
    # Twitch is optional in the same way: missing credentials or an auth
    # failure means no automatic alerts, not a dead backend. Deliberately
    # broad — nothing Twitch does should be able to stop the server booting.
    try:
        await twitch_alerts.start()
    except Exception as exc:
        print(f"[twitch] Startup failed, alerts disabled: {exc}")
    yield
    await twitch_alerts.stop()
    await obs.disconnect()


app = FastAPI(lifespan=lifespan)


# --- browser <-> backend relay (Phase 4) ------------------------------------
class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)

    async def broadcast(self, message: dict):
        for ws in self.connections:
            await ws.send_text(json.dumps(message))


manager = ConnectionManager()

# Twitch events feed straight into the same relay the control panel uses, so
# an automatic follow alert and a manually-triggered one are indistinguishable
# to the overlay. Defined here (not above lifespan) because it needs `manager`;
# lifespan only looks it up when the server actually starts.
twitch_alerts = TwitchAlerts(manager.broadcast)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# --- widget settings endpoints (Phase 8) ------------------------------------
@app.get("/api/config")
async def get_config() -> Config:
    return config


@app.put("/api/config")
async def put_config(new: Config) -> Config:
    """Replace the whole config, persist it, and push it to open overlays.

    The broadcast reuses the Phase 4 relay, so a colour change lands in OBS
    immediately — no need to refresh the browser source mid-stream.
    """
    global config
    config = new
    save_config(config)
    await manager.broadcast({"type": "config", "config": config.model_dump()})
    return config


# --- OBS control endpoints (Phase 6) ----------------------------------------
class SceneRequest(BaseModel):
    scene: str


class SourceRequest(BaseModel):
    scene: str
    source: str
    visible: bool


@app.post("/api/obs/scene")
async def set_scene(req: SceneRequest):
    """Switch OBS to a different scene."""
    await obs.call(
        simpleobsws.Request("SetCurrentProgramScene", {"sceneName": req.scene})
    )
    return {"ok": True, "scene": req.scene}


@app.post("/api/obs/source")
async def set_source_visibility(req: SourceRequest):
    """Show or hide a source within a scene.

    OBS's SetSceneItemEnabled wants a numeric sceneItemId, not a name, so we
    look the id up first with GetSceneItemId — that way the frontend only has
    to know the human-readable source name.
    """
    id_resp = await obs.call(
        simpleobsws.Request(
            "GetSceneItemId", {"sceneName": req.scene, "sourceName": req.source}
        )
    )
    item_id = id_resp.responseData["sceneItemId"]
    await obs.call(
        simpleobsws.Request(
            "SetSceneItemEnabled",
            {
                "sceneName": req.scene,
                "sceneItemId": item_id,
                "sceneItemEnabled": req.visible,
            },
        )
    )
    return {"ok": True, "source": req.source, "visible": req.visible}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = json.loads(await ws.receive_text())
            # For now: relay everything to everyone
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(ws)


# --- serve the built frontend (Phase 8) -------------------------------------
# Mounted LAST, at "/", so every route above still wins. In development this
# directory doesn't exist and the block is skipped — you keep using Vite on
# :5173, which proxies /api and /ws back here.
DIST = Path(__file__).parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html instead of 404ing.

    /control and /overlay/alert are client-side routes with no matching file
    on disk, so a plain mount 404s on a hard refresh. Handing unknown paths to
    index.html lets vue-router take over once the page boots.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # StaticFiles signals a miss by raising, not by returning a 404
            # response — so this has to be a try/except, not a status check.
            if exc.status_code != 404:
                raise
            # Let unmatched API paths 404 properly. Without this a typo'd
            # endpoint returns index.html, and the caller's res.json() fails
            # on "<!DOCTYPE html>" instead of reporting a missing route.
            if path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


if DIST.is_dir():
    app.mount("/", SPAStaticFiles(directory=DIST, html=True), name="spa")
else:
    print(f"[static] No build at {DIST} — run 'npm run build' to serve the UI.")
