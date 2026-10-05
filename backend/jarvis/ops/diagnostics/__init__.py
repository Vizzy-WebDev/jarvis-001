"""Self-diagnosis: real checks, and one automatic attempt at fixing what they find."""

from .registry import Check, get_check, list_checks, register_check, reset

__all__ = ["Check", "get_check", "list_checks", "register_check", "reset"]
