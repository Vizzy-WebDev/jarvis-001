"""The one convention shared between the prompt builder and whatever consumes the prompt.

A dependency-free leaf on purpose: `prompt.py` builds instructions, the model
boundary (`models/client.py`) reads them, and neither may import the other.

`Instructions` is the system instruction as labelled sections — persona, rules,
context — plus the label of the last section that is stable from turn to turn. It
is also a plain string (every section's text joined), so anything that only ever
treated the instruction as text keeps working. The model layer renders the
sections the way the chosen model's family reads best, and turns the stable
prefix into that provider's own cache marker.
"""

from __future__ import annotations


class Instructions(str):
    sections: tuple[tuple[str, str], ...]
    stable_prefix_until: str | None

    def __new__(cls, sections: list[tuple[str, str]] | tuple[tuple[str, str], ...],
                stable_prefix_until: str | None = None) -> "Instructions":
        kept = tuple((label, text) for label, text in sections if text)
        self = super().__new__(cls, "\n\n".join(text for _, text in kept))
        self.sections = kept
        labels = [label for label, _ in kept]
        self.stable_prefix_until = stable_prefix_until if stable_prefix_until in labels else None
        return self

    def plus(self, label: str, text: str) -> "Instructions":
        """A section added at the end (after the stable prefix)."""
        return Instructions([*self.sections, (label, text)], self.stable_prefix_until)


def with_section(system: str, label: str, text: str) -> str:
    """Add a section to an instruction, whichever kind it is."""
    if isinstance(system, Instructions):
        return system.plus(label, text)
    return f"{system}\n\n{text}" if system else text
