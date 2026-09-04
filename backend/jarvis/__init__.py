"""Jarvis — a local voice/text personal assistant.

The Python/FastAPI backend, ported from the original Node/Express implementation
in server/. During the migration both implementations read and write the same
data/ directory, the same .env and the same SQLite database, so anything here
that touches persistence is format-compatible with its Node counterpart by
design, not by coincidence.
"""

__version__ = "0.1.0"
