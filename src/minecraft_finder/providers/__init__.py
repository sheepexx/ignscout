"""Provider registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import Provider, ProviderError, ProviderInfo, ProviderRole, ProviderSelfTestError
from .demo import DemoProvider
from .minecraft import MinecraftProvider
from .namemc import NameMCProvider

if TYPE_CHECKING:
    from ..config import AppConfig

PROVIDERS: dict[str, type[Provider]] = {
    MinecraftProvider.name: MinecraftProvider,
    NameMCProvider.name: NameMCProvider,
    DemoProvider.name: DemoProvider,
}
#: Providers that may drive a full scan. NameMC is enrichment-only to keep its traffic tiny.
SCAN_PROVIDERS = (MinecraftProvider.name, DemoProvider.name)


def build_provider(
    name: str,
    config: AppConfig,
    *,
    verify: bool = False,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Provider:
    if name == MinecraftProvider.name:
        return MinecraftProvider(
            config.providers.minecraft, config.scanner, client=client, token=token, verify=verify
        )
    if name == NameMCProvider.name:
        return NameMCProvider(config.providers.namemc, config.scanner, client=client)
    if name == DemoProvider.name:
        return DemoProvider(config.providers.demo, config.scanner)
    raise KeyError(f"unknown provider {name!r}")


def provider_display_name(value: str) -> str:
    """``minecraft+namemc`` -> ``Minecraft + NameMC``."""
    parts = []
    for part in value.split("+"):
        cls = PROVIDERS.get(part)
        parts.append(cls.display_name if cls else part)
    return " + ".join(parts)


__all__ = [
    "PROVIDERS",
    "SCAN_PROVIDERS",
    "DemoProvider",
    "MinecraftProvider",
    "NameMCProvider",
    "Provider",
    "ProviderError",
    "ProviderInfo",
    "ProviderRole",
    "ProviderSelfTestError",
    "build_provider",
    "provider_display_name",
]
