"""Plugin discovery, loading, lifecycle, and emit pipeline."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from geomonitor.core.buffer import RateCounter
from geomonitor.core.models import DecodeResult, Message
from geomonitor.core.plugin_base import PLUGIN_API_VERSION, PluginBase
from geomonitor.core.track_store import TrackStore

log = logging.getLogger("geomonitor.registry")


class PluginManifest:
    """Parsed plugin manifest.json."""

    def __init__(self, data: dict[str, Any], plugin_dir: Path | None = None):
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.version: str = data.get("version", "0.0.0")
        self.description: str = data.get("description", "")
        self.author: str = data.get("author", "")
        self.icon: str = data.get("icon", "circle")
        self.color: str = data.get("color", "#888888")
        self.ingestor: dict = data.get("ingestor", {})
        self.list_columns: list[dict] = data.get("list_columns", [])
        self.settings_schema: dict = data.get("settings_schema", {})
        self.entity_types: list[str] = data.get("entity_types", [])
        self.map_layer: bool = data.get("map_layer", False)
        self.supports_hex_dump: bool = data.get("supports_hex_dump", True)
        self.api_version: int = data.get("api_version", PLUGIN_API_VERSION)
        self.plugin_dir = plugin_dir
        self._raw = data

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "list_columns": self.list_columns,
            "settings_schema": self.settings_schema,
            "entity_types": self.entity_types,
            "map_layer": self.map_layer,
            "supports_hex_dump": self.supports_hex_dump,
        }


class LoadedPlugin:
    """A plugin instance paired with its manifest and state."""

    def __init__(self, instance: PluginBase, manifest: PluginManifest):
        self.instance = instance
        self.manifest = manifest
        self.enabled: bool = True
        self.running: bool = False
        self.error: str | None = None


class PluginRegistry:
    """Discovers, loads, and manages plugin lifecycle."""

    def __init__(
        self,
        rate_counter: RateCounter,
        track_store: TrackStore,
        broadcast_fn: Any = None,  # async callable: (str) -> None
        plugins_dir: Path | None = None,
    ):
        self._rate_counter = rate_counter
        self._track_store = track_store
        self._broadcast_fn = broadcast_fn
        self._plugins_dir = plugins_dir
        self._plugins: dict[str, LoadedPlugin] = {}

    @property
    def plugins(self) -> dict[str, LoadedPlugin]:
        return self._plugins

    def set_broadcast_fn(self, fn: Any) -> None:
        """Set the broadcast function after construction (avoids circular deps)."""
        self._broadcast_fn = fn

    # ----- Discovery -----

    def discover(self) -> list[str]:
        """Discover plugins from entry points and local directory. Returns plugin IDs."""
        discovered = []

        # 1. Entry points
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group="geomonitor.plugins")
            for ep in eps:
                try:
                    plugin_cls = ep.load()
                    instance = plugin_cls()
                    manifest = self._find_manifest(instance)
                    self._register(instance, manifest)
                    discovered.append(instance.plugin_id)
                    log.info("Discovered plugin via entry point: %s", instance.plugin_id)
                except Exception as exc:
                    log.warning("Failed to load entry point %s: %s", ep.name, exc)
        except Exception:
            pass  # No entry points available

        # 2. Local plugins directory
        if self._plugins_dir and self._plugins_dir.is_dir():
            for pkg_dir in sorted(self._plugins_dir.iterdir()):
                if not pkg_dir.is_dir():
                    continue
                manifest_path = pkg_dir / "manifest.json"
                init_path = pkg_dir / "__init__.py"
                if not manifest_path.exists() or not init_path.exists():
                    continue
                if any(p.manifest.plugin_dir == pkg_dir for p in self._plugins.values()):
                    continue  # Already loaded via entry point
                try:
                    manifest = self._load_manifest(manifest_path, pkg_dir)
                    if manifest.api_version != PLUGIN_API_VERSION:
                        log.warning(
                            "Plugin %s API version %d != core %d, skipping",
                            manifest.id, manifest.api_version, PLUGIN_API_VERSION,
                        )
                        continue
                    instance = self._import_local_plugin(pkg_dir)
                    self._register(instance, manifest)
                    discovered.append(instance.plugin_id)
                    log.info("Discovered local plugin: %s (%s)", instance.plugin_id, pkg_dir)
                except Exception as exc:
                    log.warning("Failed to load local plugin %s: %s", pkg_dir.name, exc)

        return discovered

    def _find_manifest(self, instance: PluginBase) -> PluginManifest:
        """Find manifest.json in the plugin's package directory."""
        module = type(instance).__module__
        mod = importlib.import_module(module)
        pkg_dir = Path(mod.__file__).parent
        manifest_path = pkg_dir / "manifest.json"
        if manifest_path.exists():
            return self._load_manifest(manifest_path, pkg_dir)
        # Fallback: generate minimal manifest from class properties
        return PluginManifest({
            "id": instance.plugin_id,
            "name": instance.name,
        }, pkg_dir)

    def _load_manifest(self, path: Path, pkg_dir: Path) -> PluginManifest:
        with open(path) as f:
            data = json.load(f)
        return PluginManifest(data, pkg_dir)

    def _import_local_plugin(self, pkg_dir: Path) -> PluginBase:
        """Import a local plugin package and return its plugin instance."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            f"geomonitor.plugins.{pkg_dir.name}",
            pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Find PluginBase subclass in the module
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, PluginBase)
                and attr is not PluginBase
            ):
                return attr()
        raise ValueError(f"No PluginBase subclass found in {pkg_dir}")

    def _register(self, instance: PluginBase, manifest: PluginManifest) -> None:
        pid = instance.plugin_id
        if pid in self._plugins:
            log.warning("Duplicate plugin ID %s, skipping", pid)
            return
        self._plugins[pid] = LoadedPlugin(instance, manifest)

    # ----- Lifecycle -----

    async def start_all(self, settings_getter) -> None:
        """Start all enabled plugins."""
        for pid, loaded in self._plugins.items():
            if not loaded.enabled:
                continue
            try:
                settings = settings_getter(pid)
                emit = self._make_emit(loaded.instance)
                await loaded.instance.start(settings, emit)
                loaded.running = True
                log.info("Started plugin: %s", pid)
            except Exception as exc:
                loaded.error = str(exc)
                log.error("Failed to start plugin %s: %s", pid, exc)

    async def stop_all(self) -> None:
        """Stop all running plugins."""
        for pid, loaded in self._plugins.items():
            if loaded.running:
                try:
                    await loaded.instance.stop()
                    loaded.running = False
                    log.info("Stopped plugin: %s", pid)
                except Exception as exc:
                    log.error("Error stopping plugin %s: %s", pid, exc)

    async def restart_plugin(self, plugin_id: str, settings: dict) -> None:
        """Stop and restart a single plugin with new settings."""
        loaded = self._plugins.get(plugin_id)
        if not loaded:
            return
        if loaded.running:
            await loaded.instance.stop()
            loaded.running = False
        emit = self._make_emit(loaded.instance)
        await loaded.instance.start(settings, emit)
        loaded.running = True

    # ----- Emit pipeline -----

    def _make_emit(self, plugin: PluginBase):
        """Create the emit callback for a plugin."""

        async def emit(raw: bytes, src: tuple[str, int]) -> None:
            t0 = time.monotonic()
            ts = time.time()

            # Decode
            try:
                result = plugin.decode(raw, src)
            except Exception as exc:
                result = DecodeResult(decoded=None, summary="DECODE ERROR", meta={}, error=str(exc))

            # Normalize
            entities = []
            if result.decoded is not None:
                try:
                    entities = plugin.normalize(result.decoded, result.meta)
                except Exception as exc:
                    log.debug("Normalize error in %s: %s", plugin.plugin_id, exc)

            # Build message
            msg = Message(
                seq=self._rate_counter.next_seq(),
                ts=ts,
                plugin_id=plugin.plugin_id,
                src_ip=src[0],
                src_port=src[1],
                raw=raw,
                decoded=result.decoded,
                decode_error=result.error,
                summary=result.summary,
                normalized=entities if entities else None,
                meta=result.meta,
            )

            if entities:
                self._track_store.update(entities)

            # Broadcast (non-blocking queue)
            if self._broadcast_fn:
                self._broadcast_fn(msg)

            # Count with emit duration for perf tracking
            emit_time = time.monotonic() - t0
            self._rate_counter.count(plugin.plugin_id, emit_time)

        return emit

    # ----- Info -----

    def get_plugin_metadata(self) -> dict[str, Any]:
        """Plugin metadata for WebSocket snapshot."""
        result = {}
        for pid, loaded in self._plugins.items():
            meta = loaded.manifest.to_dict()
            meta["enabled"] = loaded.enabled
            meta["running"] = loaded.running
            meta["error"] = loaded.error
            result[pid] = meta
        return result
