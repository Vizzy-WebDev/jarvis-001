"""Reading Office documents. Independently written from the writers on purpose:
a round trip through the same code proves nothing."""

from .reader import read_docx, read_document, read_xlsx

__all__ = ["read_docx", "read_document", "read_xlsx"]
