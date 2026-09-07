"""Reading Office documents.

Two readers on purpose. `reader.py` is minimal and independently written — it is
what the artifact writers are VERIFIED against, and a round trip through the
same code proves nothing. `office.py` is the richer one everything else uses:
Markdown, multiple sheets, slide order, embedded images, a row budget.
"""

from .office import (
    docx_to_markdown, extract_document, is_office_document, pptx_to_markdown,
    sheet_names, sheet_rows, xlsx_to_markdown,
)
from .reader import read_docx, read_document, read_xlsx

__all__ = [
    "docx_to_markdown", "extract_document", "is_office_document", "pptx_to_markdown",
    "read_docx", "read_document", "read_xlsx", "sheet_names", "sheet_rows",
    "xlsx_to_markdown",
]
