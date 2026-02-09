"""Provider registry and factory.

Providers are registered by name and instantiated via :func:`get_provider`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from auralake.core.exceptions import ProviderNotFoundError

if TYPE_CHECKING:
    from auralake.models.config import AuraLakeConfig
    from auralake.providers.base import AbstractProvider

_REGISTRY: dict[str, type[AbstractProvider]] = {}


def register_provider(name: str, cls: type[AbstractProvider]) -> None:
    _REGISTRY[name] = cls


def get_provider(name: str, config: AuraLakeConfig) -> AbstractProvider:
    cls = _REGISTRY.get(name)
    if cls is None:
        available = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ProviderNotFoundError(name, f"Available providers: {available}")
    return cls(config)


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
