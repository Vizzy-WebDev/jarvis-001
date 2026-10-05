"""Intent routing (§11) and the fast path (§10)."""

from .router import FastPath, Intent, Route, classify

__all__ = ["FastPath", "Intent", "Route", "classify"]
