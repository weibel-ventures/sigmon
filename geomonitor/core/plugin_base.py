"""Abstract base class for GeoMonitor plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from geomonitor.core.models import DecodeResult, NormalizedEntity

# Callback type: plugin calls emit(raw_bytes, (src_ip, src_port))
EmitCallback = Callable[[bytes, tuple[str, int]], Awaitable[None]]

PLUGIN_API_VERSION = 1


class PluginBase(ABC):
    """Base class for all GeoMonitor plugins.

    Each plugin teaches the platform how to receive, decode, normalize,
    and display a specific surveillance or C2 protocol.
    """

    # --- Metadata (can be overridden or read from manifest.json) ---

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Unique plugin identifier (e.g. 'asterix'). Lowercase, [a-z0-9_-]."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable display name (e.g. 'ASTERIX')."""

    # --- Lifecycle ---

    @abstractmethod
    async def start(self, settings: dict[str, Any], emit: EmitCallback) -> None:
        """Start the plugin's ingestor(s).

        Called once at startup or when re-enabled. The plugin should:
        1. Read its settings (ports, hosts, etc.)
        2. Start asyncio tasks/transports for data reception
        3. For each received datagram/message, call:
              await emit(raw_bytes, (src_ip, src_port))

        Args:
            settings: Plugin-specific settings dict from SettingsManager.
            emit: Async callback to push raw received data to the core pipeline.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the plugin's ingestor(s). Release ports, cancel tasks.

        Must be idempotent — safe to call even if not started.
        """

    # --- Decode ---

    @abstractmethod
    def decode(self, raw: bytes, src: tuple[str, int]) -> DecodeResult:
        """Decode raw bytes into protocol-native structure.

        Produces a rich, nested dict (or list of dicts) for the decoded
        tree panel. Called synchronously — must be fast.

        Args:
            raw: Raw bytes as received from the network.
            src: (ip, port) tuple of the sender.

        Returns:
            DecodeResult with decoded data, summary, and metadata.
        """

    # --- Normalize (optional) ---

    def normalize(self, decoded: Any, meta: dict[str, Any]) -> list[NormalizedEntity]:
        """Convert decoded data to common NormalizedEntity model.

        Override to place entities on the map and enable cross-protocol
        correlation. Default returns empty list.

        Args:
            decoded: Protocol-native decoded structure from decode().
            meta: Metadata dict from decode().

        Returns:
            List of NormalizedEntity objects extracted from this message.
        """
        return []

    # --- Self-test ---

    def self_test(self) -> list[tuple[str, bool, str]]:
        """Run built-in verification of the decode pipeline.

        Called at startup before the plugin begins ingesting live data.
        Each test returns (name, passed, detail). If any test fails,
        the plugin is marked with an error but still starts.

        Override to add protocol-specific test vectors.

        Returns:
            List of (test_name, passed, detail_message) tuples.
        """
        return []

    # --- Endpoints ---

    def get_endpoints(self, settings: dict[str, Any]) -> list[str]:
        """Return human-readable descriptions of the network endpoints
        this plugin listens on or connects to, given the current settings.

        Examples: ["udp://0.0.0.0:23401", "tcp→192.168.0.84:30003"]

        Override in each plugin.
        """
        return []

    # --- Settings ---

    def on_settings_changed(self, old: dict[str, Any], new: dict[str, Any]) -> bool:
        """Called when the user changes this plugin's settings.

        Returns:
            True if the ingestor needs a restart (e.g. port changed).
            False if the change can be applied live.
        """
        return True
