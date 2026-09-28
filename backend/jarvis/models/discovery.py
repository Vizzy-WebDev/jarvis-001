"""Asking a connection what it offers: its provider module's model list, with what
the connection's declared gateway (if any) says about each model laid on top.

The one place the two are put together, so the routes (and anything else that wants a
refreshed list) ask for a connection's models without knowing which gateway it is.
"""

from __future__ import annotations

from . import gateways, providers, selection, store
from .types import Discovered


def discover(connection: store.Connection) -> list[Discovered]:
    """May raise `ProviderError` (including `Unsupported`), exactly as the provider did."""
    found = providers.for_format(connection.format).discover(selection.target_for(connection))
    return gateways.annotate(connection.gateway_kind, found)
