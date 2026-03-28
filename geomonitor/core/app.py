"""GeoMonitor — FastAPI application with plugin-based architecture."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from geomonitor.core.buffer import RingBuffer
from geomonitor.core.plugin_registry import PluginRegistry
from geomonitor.core.settings import SettingsManager
from geomonitor.core.track_store import TrackStore
from geomonitor.core.ws_manager import WSManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("geomonitor")

# ---------------------------------------------------------------------------
# Globals (initialized in lifespan)
# ---------------------------------------------------------------------------

settings_mgr: SettingsManager = None  # type: ignore
buffer: RingBuffer = None  # type: ignore
track_store: TrackStore = None  # type: ignore
ws_manager: WSManager = None  # type: ignore
registry: PluginRegistry = None  # type: ignore


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def rate_updater():
    while True:
        buffer.update_rate()
        await asyncio.sleep(1.0)


async def track_broadcaster():
    """Periodically broadcast dirty track updates to WebSocket clients."""
    while True:
        await asyncio.sleep(2.0)
        updates = track_store.flush_dirty()
        if updates and ws_manager.count > 0:
            await ws_manager.broadcast_tracks(updates)
        # Expire stale tracks every cycle
        track_store.expire_stale()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global settings_mgr, buffer, track_store, ws_manager, registry

    # Init settings
    settings_path = Path(os.environ.get("SETTINGS_FILE", "settings.json"))
    settings_mgr = SettingsManager(settings_path)

    # Override from env vars
    if os.environ.get("WEB_PORT"):
        settings_mgr.set_global("web_port", int(os.environ["WEB_PORT"]))
    if os.environ.get("BUFFER_MAX_MESSAGES"):
        settings_mgr.set_global("buffer_max_messages", int(os.environ["BUFFER_MAX_MESSAGES"]))

    global_cfg = settings_mgr.get_global()

    # Init core components
    buffer = RingBuffer(maxlen=global_cfg["buffer_max_messages"])
    track_store = TrackStore(
        history_depth=global_cfg.get("track_history_depth", 100),
        stale_seconds=global_cfg.get("track_stale_seconds", 300),
    )
    ws_manager = WSManager()

    # Init plugin registry
    plugins_dir = Path(__file__).parent.parent / "plugins"
    registry = PluginRegistry(buffer, track_store, plugins_dir=plugins_dir)
    registry.set_broadcast_fn(ws_manager.broadcast_message)

    # Discover and start plugins
    discovered = registry.discover()
    log.info("Discovered %d plugin(s): %s", len(discovered), ", ".join(discovered))

    # Initialize default settings from plugin schemas
    for pid, loaded in registry.plugins.items():
        settings_mgr.init_plugin_defaults(pid, loaded.manifest.settings_schema)
        loaded.enabled = settings_mgr.is_plugin_enabled(pid)

    # Start plugins
    await registry.start_all(settings_mgr.get_plugin)

    # Background tasks
    rate_task = asyncio.create_task(rate_updater())
    track_task = asyncio.create_task(track_broadcaster())

    yield

    # Shutdown
    rate_task.cancel()
    track_task.cancel()
    await registry.stop_all()
    log.info("GeoMonitor shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="GeoMonitor", lifespan=lifespan)


# ----- HTML -----

@app.get("/", response_class=HTMLResponse)
async def index():
    # Serve from static/ relative to this file's parent package
    html_path = Path(__file__).parent.parent / "static" / "index.html"
    if not html_path.exists():
        # Fallback: old location
        html_path = Path(__file__).parent.parent.parent / "app" / "static" / "index.html"
    return HTMLResponse(html_path.read_text())


# ----- REST API -----

@app.get("/api/stats")
async def stats():
    plugin_stats = {}
    for pid, loaded in registry.plugins.items():
        plugin_stats[pid] = {
            "name": loaded.manifest.name,
            "enabled": loaded.enabled,
            "running": loaded.running,
            "error": loaded.error,
            "messages_per_sec": round(buffer.plugin_rate(pid), 1),
        }
    return {
        "total_messages": buffer.total_seq,
        "buffer_size": len(buffer),
        "buffer_max": buffer.maxlen,
        "connected_clients": ws_manager.count,
        "messages_per_sec": round(buffer.rate, 1),
        "tracks": len(track_store),
        "plugins": plugin_stats,
    }


@app.get("/api/plugins")
async def get_plugins():
    return registry.get_plugin_metadata()


@app.get("/api/settings")
async def get_settings():
    return settings_mgr.to_dict()


@app.put("/api/settings/{plugin_id}")
async def update_settings(plugin_id: str, body: dict[str, Any]):
    loaded = registry.plugins.get(plugin_id)
    if not loaded:
        return {"error": "Plugin not found"}, 404

    old = settings_mgr.get_plugin(plugin_id)
    new = settings_mgr.update_plugin(plugin_id, body)

    # Check if restart needed
    needs_restart = loaded.instance.on_settings_changed(old, new)
    if needs_restart and loaded.running:
        await registry.restart_plugin(plugin_id, new)

    return {"ok": True, "settings": new, "restarted": needs_restart}


@app.get("/api/tracks")
async def get_tracks():
    return track_store.snapshot()


# ----- WebSocket -----

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        # Send full snapshot
        await ws_manager.send_snapshot(
            ws,
            messages=buffer.snapshot(),
            plugins=registry.get_plugin_metadata(),
            tracks=track_store.snapshot(),
        )

        # Keep alive — listen for client messages
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
                await _handle_client_message(msg)
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)


async def _handle_client_message(msg: dict[str, Any]) -> None:
    """Handle incoming WebSocket messages from clients."""
    msg_type = msg.get("type")

    if msg_type == "settings":
        plugin_id = msg.get("plugin_id")
        new_settings = msg.get("settings", {})
        if plugin_id and plugin_id in registry.plugins:
            old = settings_mgr.get_plugin(plugin_id)
            new = settings_mgr.update_plugin(plugin_id, new_settings)
            loaded = registry.plugins[plugin_id]
            if loaded.instance.on_settings_changed(old, new) and loaded.running:
                await registry.restart_plugin(plugin_id, new)

    elif msg_type == "plugin_toggle":
        plugin_id = msg.get("plugin_id")
        enabled = msg.get("enabled", True)
        if plugin_id and plugin_id in registry.plugins:
            loaded = registry.plugins[plugin_id]
            loaded.enabled = enabled
            settings_mgr.update_plugin(plugin_id, {"enabled": enabled})
            if enabled and not loaded.running:
                settings = settings_mgr.get_plugin(plugin_id)
                emit = registry._make_emit(loaded.instance)
                await loaded.instance.start(settings, emit)
                loaded.running = True
            elif not enabled and loaded.running:
                await loaded.instance.stop()
                loaded.running = False
