"""Thinking a server wrote INTO the reply text, taken back out.

Some servers put a model's reasoning in the reply's own text, opened with `<think>`, instead of
in a reasoning field. Left alone it reaches the person, the saved conversation, a job's notes
and everything that reads the reply. This turns that text back into what it is — reasoning —
as the reply streams.

What was measured (three direct probes and 86 stored replies), and so what this handles:

- The reply BEGINS with `<think>` (after optional whitespace). Text that merely mentions the
  tag later on is never touched.
- Normally the thinking ends with `</think>`.
- When the request declares tools, the same server drops the closing tag and leaves only the
  blank lines that surrounded it: the thinking then ends at the first run of three newlines.
  Some replies hold more than one such gap, and the answer does not always start after the
  last; cutting at the FIRST can let a little thinking through as text, but can never swallow
  part of the answer — and losing the answer is the worse mistake.
- Sometimes there is no end at all: the model thought, then called a tool (nothing is lost,
  it was all thinking), or the whole reply sits inside the tag (`finish()` then hands the
  text back, because a reply with nothing in it is worse than one with a stray tag).

Pure text in, pieces out: no driver, wire or app import.
"""

from __future__ import annotations

OPEN, CLOSE, GAP = "<think>", "</think>", "\n\n\n"

Piece = tuple[str, str]  # ("reasoning" | "text", the characters)


class InlineThinking:
    """Feed it the reply's text pieces in order; it gives back each piece labelled."""

    def __init__(self) -> None:
        self._mode = "start"       # start → thinking → text, or start → text
        self._held = ""            # not yet decided / not yet safe to release
        self._thought: list[str] = []
        self._trim = False         # the blank lines right after the thinking are not the answer

    @property
    def thinking(self) -> bool:
        """Still inside thinking that has not ended."""
        return self._mode == "thinking"

    def feed(self, piece: str) -> list[Piece]:
        if not piece:
            return []
        if self._mode == "text":
            if self._trim:
                piece = piece.lstrip("\n")
                self._trim = not piece
            return [("text", piece)] if piece else []
        self._held += piece
        out: list[Piece] = []
        if self._mode == "start":
            lead = self._held.lstrip()
            if lead.startswith(OPEN):
                self._mode, self._held = "thinking", lead[len(OPEN):]
            elif OPEN.startswith(lead):
                return []          # nothing but whitespace or the start of the tag so far
            else:
                self._mode, text, self._held = "text", self._held, ""
                return [("text", text)]
        # thinking: release what cannot be part of an ending, look for the ending in the rest
        # A closing tag in hand settles it; the gap is only the fallback for a dropped tag.
        at, size = self._held.find(CLOSE), len(CLOSE)
        if at < 0:
            at, size = self._held.find(GAP), len(GAP)
        if at >= 0:
            thought, rest = self._held[:at], self._held[at + size:].lstrip("\n")
            self._mode, self._held, self._trim = "text", "", not rest
            if thought:
                self._thought.append(thought)
                out.append(("reasoning", thought))
            if rest:
                out.append(("text", rest))
            return out
        keep = _possible_ending(self._held)
        ready, self._held = self._held[:len(self._held) - keep], self._held[len(self._held) - keep:]
        if ready:
            self._thought.append(ready)
            out.append(("reasoning", ready))
        return out

    def finish(self, *, called_tools: bool) -> list[Piece]:
        """The reply has ended. Whatever was held back is released; and a reply that was
        nothing BUT unclosed thinking, with no tool called, is handed back as its text."""
        held, self._held = self._held, ""
        if self._mode == "start":
            self._mode = "text"
            return [("text", held)] if held else []
        if self._mode != "thinking":
            return []
        self._mode = "text"
        if called_tools:
            return [("reasoning", held)] if held else []
        whole = ("".join(self._thought) + held).strip()
        return [("text", whole)] if whole else []


def _possible_ending(text: str) -> int:
    """How many characters at the end of `text` could be the start of an ending still arriving
    (a split `</think>`, or newlines that may yet become a gap)."""
    longest = 0
    for mark in (CLOSE, GAP):
        for size in range(min(len(mark) - 1, len(text)), 0, -1):
            if text.endswith(mark[:size]):
                longest = max(longest, size)
                break
    return longest
