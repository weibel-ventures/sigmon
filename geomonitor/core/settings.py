"""Settings manager with JSON persistence."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("geomonitor.settings")

DEFAULT_GLOBAL = {
    "buffer_max_messages": 50_000,
    "web_port": 8080,
    "track_history_depth": 100,
    "track_stale_seconds": 300,
}


class SettingsManager:
    """Manages global and per-plugin settings with JSON file persistence."""

    def __init__(self, path: Path | None = None):
        self._path = path or Path("settings.json")
        self._data: dict[str, Any] = {"global": dict(DEFAULT_GLOBAL), "plugins": {}}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    saved = json.load(f)
                # Merge saved over defaults
                if "global" in saved:
                    self._data["global"].update(saved["global"])
                if "plugins" in saved:
                    self._data["plugins"] = saved["plugins"]
                log.info("Loaded settings from %s", self._path)
            except Exception as exc:
                log.warning("Failed to load settings from %s: %s", self._path, exc)
        else:
            log.info("No settings file found, using defaults")

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            log.warning("Failed to save settings to %s: %s", self._path, exc)

    # ----- Global -----

    def get_global(self) -> dict[str, Any]:
        return dict(self._data["global"])

    def set_global(self, key: str, value: Any) -> None:
        self._data["global"][key] = value
        self._save()

    # ----- Per-plugin -----

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        """Get plugin settings, merged with defaults from schema."""
        return dict(self._data["plugins"].get(plugin_id, {}))

    def set_plugin(self, plugin_id: str, settings: dict[str, Any]) -> None:
        self._data["plugins"][plugin_id] = settings
        self._save()

    def update_plugin(self, plugin_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge updates into existing plugin settings. Returns new settings."""
        current = self.get_plugin(plugin_id)
        current.update(updates)
        self.set_plugin(plugin_id, current)
        return current

    def is_plugin_enabled(self, plugin_id: str) -> bool:
        return self.get_plugin(plugin_id).get("enabled", True)

    def init_plugin_defaults(self, plugin_id: str, schema: dict[str, Any]) -> None:
        """Initialize plugin settings from JSON Schema defaults if not already set."""
        current = self.get_plugin(plugin_id)
        properties = schema.get("properties", {})
        changed = False
        for key, prop in properties.items():
            if key not in current and "default" in prop:
                current[key] = prop["default"]
                changed = True
        if "enabled" not in current:
            current["enabled"] = True
            changed = True
        if changed:
            self.set_plugin(plugin_id, current)

    # ----- Serialization -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "global": self.get_global(),
            "plugins": dict(self._data["plugins"]),
        }
