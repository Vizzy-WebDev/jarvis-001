"""Turning files attached in conversation into something a turn can carry.

There are exactly two ways a file enters a conversation, and choosing between
them is the whole job here:

  INLINE      — small enough to ride inside the message itself. The model sees
                the real thing, every turn, for as long as it is in the
                transcript. Images and text documents.
  REGISTERED  — too expensive to re-send every turn AND not something to read
                automatically. Video and audio get the free glance and nothing
                more: the model is told what the attachment appears to be and
                asked what is wanted. Auto-ingesting on arrival would break the
                same "don't analyse before being asked" rule that governs
                everything else entering the conversation.

Images deliberately are NOT registered. They stay in the transcript at full
fidelity, so "what does the third line say?" still works ten turns later. That is
a chosen trade: a bigger request each turn, in exchange for the image genuinely
still being there.

A leaf as far as the tool loader is concerned.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .documents import extract_document, is_office_document
from .media import TooBig, file_kind, inline_attachment, mime_type_for, read_as_text
from .uploads import get_upload

logger = logging.getLogger(__name__)

#: A text document is pasted into the prompt, so it competes with the rest of the
#: conversation for room. Generous for a real document, far short of a context
#: window.
TEXT_MAX_CHARS = 40000

TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json", ".rtf", ".log", ".yaml",
                           ".yml", ".xml", ".html", ".htm"})


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= TEXT_MAX_CHARS:
        return text, False
    return text[:TEXT_MAX_CHARS], True


def _block(name: str, text: str) -> str:
    clipped, truncated = _clip(text)
    tail = ("\n--- (truncated — the file is longer than this) ---" if truncated
            else "\n--- end of file ---")
    return f'--- Contents of the attached file "{name}" ---\n{clipped}{tail}'


class Prepared:
    """What a turn's attachments add to it."""

    def __init__(self) -> None:
        self.media: list[dict[str, Any]] = []
        self.text_blocks: list[str] = []
        self.notes: list[str] = []
        self.need: dict[str, bool] = {}

    def as_dict(self) -> dict[str, Any]:
        return {"media": self.media, "textBlocks": self.text_blocks,
                "notes": self.notes, "need": self.need}


def prepare_for_turn(ids: list[str] | None, *, session_id: str) -> Prepared:
    """Prepare every attachment on one turn."""
    prepared = Prepared()

    for upload_id in ids or []:
        upload = get_upload(upload_id)
        if upload is None:
            prepared.notes.append("One of the attached files could not be found any more — "
                                  "it may have been cleared.")
            continue

        path = Path(upload["path"])
        name = upload["name"]
        kind = file_kind(name)
        mime_type = mime_type_for(name)

        # --- images: inline, and they stay ---
        if kind == "image":
            try:
                prepared.media.extend(inline_attachment(path, mime_type))
                prepared.need["vision"] = True
            except TooBig as big:
                prepared.notes.append(
                    f'"{name}" is too large to send directly '
                    f"({round(big.size / 1024 / 1024)}MB). A smaller copy would work.")
            except OSError:
                prepared.notes.append(f'"{name}" couldn\'t be read.')
            continue

        # --- text documents: straight into the prompt, no capability needed ---
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = read_as_text(path)
            if text is None:
                prepared.notes.append(f'"{name}" couldn\'t be read as text.')
            else:
                prepared.text_blocks.append(_block(name, text))
            continue

        # --- PDFs: only a rich-media model takes one; say so plainly otherwise ---
        if mime_type == "application/pdf":
            if not _can_take_rich_files():
                prepared.notes.append(
                    f'"{name}" is a PDF, and none of the available models can read one '
                    "directly right now. A Gemini model can. Tell the user that rather than "
                    "guessing at the contents.")
                continue
            try:
                prepared.media.extend(inline_attachment(path, mime_type))
                # The "can take a rich file" ceiling, not literally video.
                prepared.need["video"] = True
            except TooBig:
                prepared.notes.append(f'"{name}" is too large to send directly.')
            except OSError:
                prepared.notes.append(f'"{name}" couldn\'t be read.')
            continue

        # --- Word / Excel / PowerPoint: real structure, not a binary dump ---
        # No capability gate: this becomes Markdown text, so every model can read
        # one, exactly like a plain text document.
        if is_office_document(name):
            document = extract_document(path)
            if not document["ok"]:
                prepared.notes.append(f'"{name}" couldn\'t be read: {document["error"]}')
                continue
            prepared.text_blocks.append(_block(name, document["markdown"]))
            if document.get("note"):
                # analyze_spreadsheet needs this exact id to find the file again —
                # the same upload-id space the browser already uses, not a new one.
                prepared.notes.append(f'About "{name}": {document["note"]} '
                                      f"(reference id for analyze_spreadsheet: {upload_id})")
            if document.get("images"):
                prepared.media.extend(document["images"])
                prepared.need["vision"] = True
            continue

        # --- video and audio: registered only, never read or watched here ---
        if kind in ("video", "audio"):
            prepared.notes.append(_register(path, name, session_id))
            continue

        # --- last resort: is this actually text, whatever its extension? ---
        text = read_as_text(path)
        if text is not None:
            prepared.text_blocks.append(_block(name, text))
            continue

        prepared.notes.append(f'"{name}" isn\'t a kind of file that can be read.')

    return prepared


def _can_take_rich_files() -> bool:
    from .gateway.routing import Task, build_candidates

    return bool(build_candidates(Task(text="", needs_tools=False, need={"video": True})))


def _register(path: Path, name: str, session_id: str) -> str:
    """The free glance, and a note saying plainly that nothing was read."""
    from .content.intake import describe
    from .content.investigator import share

    try:
        shared = share(source={"kind": "file", "filePath": str(path)}, session_id=session_id)
    except Exception as err:  # noqa: BLE001 — a failed registration is a note, not a crash
        logger.exception("could not register %s", name)
        return f'"{name}" couldn\'t be registered: {err}.'

    return (f'The user attached "{name}" — {describe(shared["identity"])}. It has NOT been '
            f'read or watched — reference id: {shared["record"]["id"]}. If they already said '
            f"what they want done with it in this message, use examine_content with that as "
            f"the request. Otherwise say plainly what it appears to be and ask what they "
            f"want, the same as for anything else shared.")


def compose_message(user_text: str, prepared: Prepared | None) -> str:
    """Fold document text and notes into the user's message, so a model with no
    media support still receives everything that could be read as words."""
    if prepared is None:
        return user_text
    parts = []
    if prepared.text_blocks:
        parts.append("\n\n".join(prepared.text_blocks))
    if prepared.notes:
        parts.append(f"[Note for you, not spoken by the user: {' '.join(prepared.notes)}]")
    if user_text:
        parts.append(user_text)
    return "\n\n".join(parts).strip()
