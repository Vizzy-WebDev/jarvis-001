"""Reaching things beyond this machine: a folder, a server, an API, a command.

Five mechanisms, in two groups. `files` is Jarvis's own built-in ability — a
singleton with an allowlist that grows only through conversation, never a
connector the user adds from a list. `mcp`, `api` and `cli` are the three peer
mechanisms a user connects things through; none is the real one with the others
bolted on, and every tool from any of them goes through the same permission
filter and the same risk check before it ever reaches a model.

The visible browser connector is deliberately not here: it drives a real window
on a real desktop, and it lands with the desktop work, where it can be tested as
it actually runs rather than only headlessly.
"""

from .store import (
    add_connector, delete_connector, get_connector, get_or_create_singleton,
    list_connectors, update_connector,
)

__all__ = ["add_connector", "delete_connector", "get_connector",
           "get_or_create_singleton", "list_connectors", "update_connector"]
