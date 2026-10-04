"""Configuration: built-in defaults < ``config.toml`` < command-line flags."""

from __future__ import annotations

import dataclasses
import tomllib
import typing
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import DEFAULT_USER_AGENT

DEFAULT_CONFIG_FILE = "config.toml"

# Credentials never belong in a config file that might be committed or shared.
SECRET_KEYS = frozenset(
    {"token", "access_token", "bearer", "password", "cookie", "cookies", "api_key", "secret"}
)


class ConfigError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


@dataclass
class ScannerConfig:
    workers: int = 4
    requests_per_second: float = 1.0
    burst: int = 1
    timeout: float = 10.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    rate_limit_cooldown: float = 30.0
    filter_offensive: bool = True
    user_agent: str = DEFAULT_USER_AGENT

    def validate(self) -> None:
        _require(1 <= self.workers <= 64, "scanner.workers must be between 1 and 64")
        _require(
            0 < self.requests_per_second <= 50,
            "scanner.requests_per_second must be greater than 0 and at most 50",
        )
        _require(1 <= self.burst <= 10, "scanner.burst must be between 1 and 10")
        _require(0 < self.timeout <= 120, "scanner.timeout must be between 0 and 120 seconds")
        _require(0 <= self.max_retries <= 10, "scanner.max_retries must be between 0 and 10")
        _require(
            0 < self.backoff_base <= self.backoff_max,
            "scanner.backoff_base must be > 0 and not larger than scanner.backoff_max",
        )
        _require(self.rate_limit_cooldown >= 1, "scanner.rate_limit_cooldown must be >= 1 second")
        _require(bool(self.user_agent.strip()), "scanner.user_agent must not be empty")


@dataclass
class CacheConfig:
    ttl_hours: float = 24.0

    @property
    def ttl(self) -> timedelta:
        return timedelta(hours=self.ttl_hours)

    def validate(self) -> None:
        _require(self.ttl_hours >= 0, "cache.ttl_hours must not be negative")


@dataclass
class OutputConfig:
    directory: str = "output"
    jsonl: bool = True

    def validate(self) -> None:
        _require(bool(self.directory.strip()), "output.directory must not be empty")


@dataclass
class DatabaseConfig:
    path: str = "data/results.db"

    def validate(self) -> None:
        _require(bool(self.path.strip()), "database.path must not be empty")


@dataclass
class LoggingConfig:
    directory: str = "logs"

    def validate(self) -> None:
        _require(bool(self.directory.strip()), "logging.directory must not be empty")


@dataclass
class MinecraftProviderConfig:
    lookup_url: str = "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}"
    bulk_url: str = "https://api.minecraftservices.com/minecraft/profile/lookup/bulk/byname"
    availability_url: str = "https://api.minecraftservices.com/minecraft/profile/name/{name}/available"
    use_bulk: bool = True
    verify_availability: bool = False
    token_env: str = "MINECRAFT_ACCESS_TOKEN"
    verify_requests_per_minute: float = 3.0
    self_test: bool = True

    def validate(self) -> None:
        _require("{name}" in self.lookup_url, "providers.minecraft.lookup_url needs a {name} placeholder")
        _require(
            "{name}" in self.availability_url,
            "providers.minecraft.availability_url needs a {name} placeholder",
        )
        _require(
            0 < self.verify_requests_per_minute <= 4,
            "providers.minecraft.verify_requests_per_minute must be > 0 and <= 4 "
            "(Mojang allows 20 availability checks per 5 minutes)",
        )
        _require(bool(self.token_env.strip()), "providers.minecraft.token_env must not be empty")


@dataclass
class NameMCProviderConfig:
    enabled: bool = False
    base_url: str = "https://namemc.com"
    min_interval_seconds: float = 10.0
    max_consecutive_failures: int = 3

    def validate(self) -> None:
        _require(
            self.min_interval_seconds >= 5,
            "providers.namemc.min_interval_seconds must be at least 5 seconds",
        )
        _require(
            1 <= self.max_consecutive_failures <= 20,
            "providers.namemc.max_consecutive_failures must be between 1 and 20",
        )


@dataclass
class DemoProviderConfig:
    latency: float = 0.2
    error_rate: float = 0.01

    def validate(self) -> None:
        _require(0 <= self.latency <= 10, "providers.demo.latency must be between 0 and 10")
        _require(0 <= self.error_rate <= 1, "providers.demo.error_rate must be between 0 and 1")


@dataclass
class ProvidersConfig:
    default: str = "minecraft"
    minecraft: MinecraftProviderConfig = field(default_factory=MinecraftProviderConfig)
    namemc: NameMCProviderConfig = field(default_factory=NameMCProviderConfig)
    demo: DemoProviderConfig = field(default_factory=DemoProviderConfig)

    def validate(self) -> None:
        _require(
            self.default in {"minecraft", "demo"},
            "providers.default must be 'minecraft' or 'demo'",
        )
        self.minecraft.validate()
        self.namemc.validate()
        self.demo.validate()


@dataclass
class AppConfig:
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    source: str = field(default="", metadata={"internal": True})

    def validate(self) -> None:
        for section in (self.scanner, self.cache, self.output, self.database, self.logging, self.providers):
            section.validate()


def load_config(path: Path | None = None) -> tuple[AppConfig, list[str]]:
    """Load configuration. Returns the config and a list of human-readable warnings.

    Without an explicit ``path``, ``./config.toml`` is used if it exists;
    otherwise the built-in defaults apply.
    """
    if path is None:
        candidate = Path(DEFAULT_CONFIG_FILE)
        if not candidate.is_file():
            return AppConfig(), []
        path = candidate
    elif not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    config = AppConfig()
    warnings: list[str] = []
    _apply(config, data, "", warnings)
    config.source = str(path)
    config.validate()
    return config, warnings


def _apply(target: Any, data: dict[str, Any], prefix: str, warnings: list[str]) -> None:
    hints = typing.get_type_hints(type(target))
    known = {f.name for f in dataclasses.fields(target) if not f.metadata.get("internal")}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key.lower() in SECRET_KEYS:
            warnings.append(
                f"ignored '{dotted}': never store credentials in config.toml — "
                "use the environment variable named by providers.minecraft.token_env"
            )
            continue
        if key not in known:
            warnings.append(f"unknown setting '{dotted}' ignored")
            continue
        current = getattr(target, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"'{dotted}' must be a table, e.g. [{dotted}]")
            _apply(current, value, f"{dotted}.", warnings)
        else:
            setattr(target, key, _coerce(value, hints[key], dotted))


def _coerce(value: Any, hint: Any, dotted: str) -> Any:
    is_bool = isinstance(value, bool)
    if hint is bool and is_bool:
        return value
    if hint is int and isinstance(value, int) and not is_bool:
        return value
    if hint is float and isinstance(value, (int, float)) and not is_bool:
        return float(value)
    if hint is str and isinstance(value, str):
        return value
    expected = getattr(hint, "__name__", str(hint))
    raise ConfigError(f"'{dotted}' must be {expected}, got {type(value).__name__} ({value!r})")
