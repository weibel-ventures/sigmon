"""GeoMonitor — FastAPI application with plugin-based architecture."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from geomonitor.core.buffer import RateCounter
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
rate_counter: RateCounter = None  # type: ignore
track_store: TrackStore = None  # type: ignore
ws_manager: WSManager = None  # type: ignore
registry: PluginRegistry = None  # type: ignore


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def rate_updater():
    try:
        while True:
            rate_counter.update_rate()
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.error("rate_updater crashed: %s", exc, exc_info=True)


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
    global settings_mgr, rate_counter, track_store, ws_manager, registry

    # Init settings — uses /etc/sigmon/ or SIGMON_CONFIG_DIR or ./config
    config_dir = os.environ.get("SIGMON_CONFIG_DIR")
    settings_mgr = SettingsManager(Path(config_dir) if config_dir else None)

    # Override from env vars
    if os.environ.get("WEB_PORT"):
        settings_mgr.set_global("web_port", int(os.environ["WEB_PORT"]))

    log.info("Config dir: %s", settings_mgr.config_dir)

    global_cfg = settings_mgr.get_global()

    # Init core components
    rate_counter = RateCounter()
    track_store = TrackStore(
        history_depth=global_cfg.get("track_history_depth", 100),
        stale_seconds=global_cfg.get("track_stale_seconds", 300),
    )
    ws_manager = WSManager()

    # Init plugin registry
    plugins_dir = Path(__file__).parent.parent / "plugins"
    registry = PluginRegistry(rate_counter, track_store, plugins_dir=plugins_dir)
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
    ws_manager.start_flush_loop()
    rate_task = asyncio.create_task(rate_updater())
    track_task = asyncio.create_task(track_broadcaster())

    yield

    # Shutdown
    rate_task.cancel()
    track_task.cancel()
    ws_manager.stop_flush_loop()
    await registry.stop_all()
    log.info("GeoMonitor shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Signal Monitor", lifespan=lifespan)


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
            "messages_per_sec": round(rate_counter.plugin_rate(pid), 1),
            "emit_avg_ms": round(rate_counter.plugin_emit_ms(pid), 3),
        }
    return {
        "total_messages": rate_counter.total_seq,
        "connected_clients": ws_manager.count,
        "messages_per_sec": round(rate_counter.rate, 1),
        "tracks": len(track_store),
        "plugins": plugin_stats,
        "perf": ws_manager.get_perf_stats(),
    }


@app.get("/api/plugins")
async def get_plugins():
    return registry.get_plugin_metadata(settings_mgr.get_plugin)


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
    restarted = False
    if needs_restart and loaded.running:
        await registry.restart_plugin(plugin_id, new)
        restarted = True
    elif needs_restart and loaded.enabled and not loaded.running:
        # Plugin is enabled but stopped — start it with new settings
        emit = registry._make_emit(loaded.instance)
        await loaded.instance.start(new, emit)
        loaded.running = True
        restarted = True

    return {"ok": True, "settings": new, "restarted": restarted}


@app.put("/api/settings/global")
async def update_global_settings(body: dict[str, Any]):
    new = settings_mgr.update_global(body)
    return {"ok": True, "settings": new}


@app.post("/api/plugins/{plugin_id}/restart")
async def restart_plugin(plugin_id: str):
    loaded = registry.plugins.get(plugin_id)
    if not loaded:
        return {"error": "Plugin not found"}
    settings = settings_mgr.get_plugin(plugin_id)
    await registry.restart_plugin(plugin_id, settings)
    return {"ok": True, "running": loaded.running}


@app.post("/api/plugins/{plugin_id}/stop")
async def stop_plugin(plugin_id: str):
    loaded = registry.plugins.get(plugin_id)
    if not loaded:
        return {"error": "Plugin not found"}
    if loaded.running:
        await loaded.instance.stop()
        loaded.running = False
    return {"ok": True, "running": False}


@app.post("/api/plugins/{plugin_id}/enable")
async def toggle_plugin(plugin_id: str, body: dict[str, Any]):
    loaded = registry.plugins.get(plugin_id)
    if not loaded:
        return {"error": "Plugin not found"}
    enabled = body.get("enabled", True)
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
    return {"ok": True, "enabled": enabled, "running": loaded.running}


@app.get("/api/tracks")
async def get_tracks():
    return track_store.snapshot()


# ----- WebSocket -----

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        # Send plugin metadata and track state (messages are client-side only)
        await ws_manager.send_snapshot(
            ws,
            plugins=registry.get_plugin_metadata(settings_mgr.get_plugin),
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
