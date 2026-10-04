from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx
import pytest


class FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def reset_app_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_minecraft_finder_handler", False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(autouse=True)
def _cleanup_logging():
    yield
    reset_app_logging()


@pytest.fixture(autouse=True)
def _fancy_symbols(monkeypatch):
    """Render the same symbols regardless of which terminal runs the tests."""
    monkeypatch.setenv("MCF_SYMBOLS", "fancy")
