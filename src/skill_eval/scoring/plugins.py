"""Plugin registry for extensible assertion types."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

PluginHandler = Callable[[dict, Path], tuple[bool, str]]


class AssertionPluginRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, PluginHandler] = {}

    def register(self, plugin_type: str, handler: PluginHandler) -> None:
        self._handlers[plugin_type] = handler

    def get(self, plugin_type: str) -> PluginHandler | None:
        return self._handlers.get(plugin_type)

    def list_plugins(self) -> list[str]:
        return sorted(self._handlers.keys())
