"""Shared fixtures for the provider conformance suite: a real stub provider per format."""

from __future__ import annotations

import pytest

from stub_provider_server import StubProvider


@pytest.fixture
def stub_for():
    started: list[StubProvider] = []

    def make(format: str, **kwargs) -> StubProvider:
        stub = StubProvider(format, **kwargs)
        stub.start()
        started.append(stub)
        return stub

    yield make
    for stub in started:
        stub.stop()
