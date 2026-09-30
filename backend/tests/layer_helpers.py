"""Shared set-up for the model layer's tests: a scratch config of fake connections."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from jarvis.models import config, engine, state
from jarvis.models.drivers import fake
from jarvis.models.router import DefaultRouter
from jarvis.models.types import Message, Request, TextPart


@pytest.fixture
def layer(scratch):
    config.forget()
    state.reset()
    fake.reset()
    engine.router = DefaultRouter()
    yield scratch
    config.forget()
    state.reset()
    fake.reset()


def fake_conn(name: str, *, trust: str = "local", models: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {"name": name, "driver": "fake", "base_url": f"http://{name}.test", "trust": trust,
            "models": models if models is not None else {"m": {}}, **kw}


def configure(scratch, connections: list[dict[str, Any]], **sections: Any) -> config.Config:
    doc = {"connections": connections, **sections}
    (scratch.data_dir / "models.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    config.forget()
    return config.current()


def ask(text: str = "hello", **kw: Any) -> Request:
    kw.setdefault("task_class", "chat")
    kw.setdefault("data_class", "personal")
    return Request(items=(Message("user", (TextPart(text),)),), **kw)


CHAT = {"text_in": True, "tools": True}
