"""Settings manager with directory-based config persistence.

Config layout:
  {config_dir}/sigmon.conf          — global settings (JSON)
  {config_dir}/sigmon.d/<plugin>.conf — per-plugin settings (JSON)

Default config_dir: /etc/sigmon (production) or ./config (development).
Override via SIGMON_CONFIG_DIR environment variable.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("geomonitor.settings")

DEFAULT_GLOBAL = {
    "buffer_max_messages": 50_000,
    "web_port": 8080,
    "track_history_depth": 100,
    "track_stale_seconds": 300,
}


def _get_config_dir() -> Path:
    """Determine config directory from env or defaults."""
    env = os.environ.get("SIGMON_CONFIG_DIR")
    if env:
        return Path(env)
    # Try /etc/sigmon if it exists or we can create it
    etc = Path("/etc/sigmon")
    if etc.exists():
        return etc
    # Fallback to local config/ directory
    return Path("config")


class SettingsManager:
    """Manages global and per-plugin settings with directory-based persistence.

    File layout:
        config_dir/sigmon.conf           — global JSON
        config_dir/sigmon.d/<plugin>.conf — per-plugin JSON
    """

    def __init__(self, config_dir: Path | None = None):
        self._dir = config_dir or _get_config_dir()
        self._plugins_dir = self._dir / "sigmon.d"
        self._global_path = self._dir / "sigmon.conf"
        self._global: dict[str, Any] = dict(DEFAULT_GLOBAL)
        self._plugins: dict[str, dict[str, Any]] = {}

        # Legacy: migrate from single settings.json if it exists
        self._migrate_legacy()

        self._ensure_dirs()
        self._load_global()
        self._load_all_plugins()

    def _ensure_dirs(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            log.warning("Cannot create config dir %s — running with defaults", self._dir)

    def _migrate_legacy(self) -> None:
        """Migrate from old single settings.json to new directory layout."""
        legacy = Path("settings.json")
        if not legacy.exists():
            return
        try:
            with open(legacy) as f:
                data = json.load(f)
            if "global" in data:
                self._global.update(data["global"])
            if "plugins" in data:
                for pid, psettings in data["plugins"].items():
                    self._plugins[pid] = psettings
            log.info("Migrated settings from legacy settings.json")
            # Write to new locations
            self._ensure_dirs()
            self._save_global()
            for pid in self._plugins:
                self._save_plugin(pid)
            # Don't delete legacy file — user can do that manually
        except Exception as exc:
            log.debug("Legacy migration skipped: %s", exc)

    # ----- Load -----

    def _load_global(self) -> None:
        if self._global_path.exists():
            try:
                with open(self._global_path) as f:
                    saved = json.load(f)
                self._global.update(saved)
                log.info("Loaded global config from %s", self._global_path)
            except Exception as exc:
                log.warning("Failed to load %s: %s", self._global_path, exc)

    def _load_all_plugins(self) -> None:
        if not self._plugins_dir.exists():
            return
        for conf in sorted(self._plugins_dir.glob("*.conf")):
            pid = conf.stem
            try:
                with open(conf) as f:
                    self._plugins[pid] = json.load(f)
                log.info("Loaded plugin config: %s", conf)
            except Exception as exc:
                log.warning("Failed to load %s: %s", conf, exc)

    # ----- Save -----

    def _save_global(self) -> None:
        try:
            with open(self._global_path, "w") as f:
                json.dump(self._global, f, indent=2)
                f.write("\n")
        except Exception as exc:
            log.warning("Failed to save %s: %s", self._global_path, exc)

    def _save_plugin(self, plugin_id: str) -> None:
        path = self._plugins_dir / f"{plugin_id}.conf"
        try:
            with open(path, "w") as f:
                json.dump(self._plugins.get(plugin_id, {}), f, indent=2)
                f.write("\n")
        except Exception as exc:
            log.warning("Failed to save %s: %s", path, exc)

    # ----- Global -----

    def get_global(self) -> dict[str, Any]:
        return dict(self._global)

    def set_global(self, key: str, value: Any) -> None:
        self._global[key] = value
        self._save_global()

    def update_global(self, updates: dict[str, Any]) -> dict[str, Any]:
        self._global.update(updates)
        self._save_global()
        return dict(self._global)

    # ----- Per-plugin -----

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return dict(self._plugins.get(plugin_id, {}))

    def set_plugin(self, plugin_id: str, settings: dict[str, Any]) -> None:
        self._plugins[plugin_id] = settings
        self._save_plugin(plugin_id)

    def update_plugin(self, plugin_id: str, updates: dict[str, Any]) -> dict[str, Any]:
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

    @property
    def config_dir(self) -> str:
        return str(self._dir)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_dir": str(self._dir),
            "global": self.get_global(),
            "plugins": {pid: dict(s) for pid, s in self._plugins.items()},
        }
