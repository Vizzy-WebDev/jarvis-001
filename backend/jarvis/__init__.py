"""Jarvis — a local voice/text personal assistant.

Ported from an original Node/Express implementation, retired at the S6 cutover.
Throughout the migration both implementations read and write the same data/
directory, the same .env and the same SQLite database, so everything here that
touches persistence is format-compatible with what the Node build wrote by
design rather than by coincidence — which is why an existing install's data
carried straight over with no conversion step.
"""

__version__ = "0.1.0"
