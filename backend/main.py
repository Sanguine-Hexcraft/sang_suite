import json
import os

from contextlib import asynccontextmanager

import simpleobsws
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

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
