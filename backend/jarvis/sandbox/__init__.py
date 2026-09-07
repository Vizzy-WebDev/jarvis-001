"""Isolated code execution — and an honest account of how isolated it actually is."""

from .runner import SandboxResult, backend, describe_isolation, run_python

__all__ = ["SandboxResult", "backend", "describe_isolation", "run_python"]
