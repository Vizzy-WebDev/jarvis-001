"""Assembling what the model actually sees, under a stated budget (§23).

§23 asks for relevance-based assembly — recent conversation, relevant long-term
memory, the current task, tool results, and a token budget — not "put everything
in the prompt". The original injects the ENTIRE approved memory set into every
call, which works only for as long as the set stays small, and stops being true
exactly when memory starts being worth having.

**Three decisions worth stating, because each trades something real:**

1. **Relevance is keyword overlap plus importance plus recency — not embeddings.**
   There is no embedding model in this stack, and adding one to rank a few dozen
   short sentences would be a dependency, a download and a second model call per
   turn for a set that fits in a prompt today. What this does instead is honest
   about being shallow, and it is measured: `selection` on the returned context
   says exactly what went in and why.

2. **Importance overrides relevance, on purpose.** A fact the user marked as
   changing how they should be helped must not drop out because the current
   sentence happens to share no words with it. Relevance decides what ELSE gets
   in, never what gets excluded from the floor.

3. **The budget is counted in estimated tokens (characters / 4), and the estimate
   is labelled as one.** A real tokeniser is per-provider, and being exactly right
   about the budget matters far less than never silently exceeding it.

Cutting the transcript is done with the same rule the conversation store uses: a
slice must never land between an assistant's tool call and its result, because
every provider rejects a transcript with an orphaned call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .. import conversation, prompt
from ..memory import store as memory_store

#: The whole context budget for one turn, in ESTIMATED tokens. Deliberately well
#: under the smallest context window on the roster: the budget exists to keep the
#: prompt purposeful, not to fill whatever space a model happens to have.
DEFAULT_BUDGET_TOKENS = 6000

#: What memory may take of it. Everything else goes to the transcript, which is
#: what the user is actually talking about right now.
MEMORY_SHARE = 0.2

#: Below this, a memory is included regardless of relevance (1-5 scale).
ALWAYS_INCLUDE_IMPORTANCE = 4

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "your", "you", "this", "that", "what",
    "when", "how", "can", "was", "were", "are", "did", "does", "have", "has",
    "about", "there", "their", "them", "they", "would", "could", "should", "just",
})


def estimate_tokens(text: str) -> int:
    """Characters / 4. An estimate, and named as one — see this module's docstring."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class AssembledContext:
    system: str
    messages: list[dict[str, Any]]
    #: What went in and why — for observability (§25), and for answering "why did
    #: you think that" with something real.
    included: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextAssembler(Protocol):
    def assemble(self, *, session_id: str, text: str) -> AssembledContext:
        ...


def _stem(word: str) -> str:
    """A crude suffix trim, not a stemmer.

    Without it "what database should I USE" and a memory saying "USES Postgres"
    share no term at all, which is a silly way to miss the one relevant fact.
    Only long-enough words are trimmed, so "uses" -> "use" but "was" stays "was".
    """
    # Minimum lengths tuned so "uses" -> "use" and "works" -> "work", while
    # "was", "this" and "is" are left alone. Both sides of a comparison go
    # through this, so an over-trim ("postgres" -> "postgr") still matches
    # itself — the failure mode that matters is under-trimming, which silently
    # loses the one relevant fact.
    for suffix, minimum in (("ing", 6), ("ed", 5), ("es", 5), ("s", 4)):
        if len(word) >= minimum and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def score_memory(memory: dict[str, Any], terms: set[str], position: int) -> float:
    """How strongly this memory belongs in THIS turn's prompt.

    Overlap dominates; importance breaks ties and lifts a genuinely important
    fact above a merely wordy one; position is a mild recency preference, since
    `list_memories` returns newest-updated first.
    """
    overlap = len(_terms(memory.get("text", "")) & terms)
    importance = memory.get("importance") or 0
    return overlap * 3 + importance * 0.5 - position * 0.01


def select_memories(text: str, memories: list[dict[str, Any]], budget_tokens: int,
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The memories worth this turn's budget, and an account of the choice."""
    terms = _terms(text)
    scored = [(score_memory(m, terms, i), i, m) for i, m in enumerate(memories)]

    floor = [m for m in memories if (m.get("importance") or 0) >= ALWAYS_INCLUDE_IMPORTANCE]
    floor_ids = {m["id"] for m in floor}
    relevant = [m for score, _, m in sorted(scored, key=lambda row: (-row[0], row[1]))
                if m["id"] not in floor_ids and score > 0]

    chosen: list[dict[str, Any]] = []
    used = 0
    for memory in floor + relevant:
        cost = estimate_tokens(memory.get("text", "")) + 8  # the "- ... (noted ...)" wrapper
        if used + cost > budget_tokens:
            continue
        chosen.append(memory)
        used += cost

    return chosen, {
        "considered": len(memories),
        "alwaysIncluded": len(floor),
        "byRelevance": len(chosen) - len([m for m in chosen if m["id"] in floor_ids]),
        "dropped": len(memories) - len(chosen),
        "tokensUsed": used,
        "tokenBudget": budget_tokens,
        "strategy": "keyword overlap + importance + recency (no embeddings — see module docs)",
    }


def trim_messages(messages: list[dict[str, Any]], budget_tokens: int) -> list[dict[str, Any]]:
    """The newest messages that fit, never cutting a tool call from its result."""
    kept: list[dict[str, Any]] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(str(message.get("text") or "")) + \
            estimate_tokens(str(message.get("toolCalls") or "")) + \
            estimate_tokens(str(message.get("toolResults") or ""))
        if used + cost > budget_tokens and kept:
            break
        kept.append(message)
        used += cost
    kept.reverse()

    # A transcript that STARTS with tool results has lost the assistant message
    # that requested them; every provider rejects that outright.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept


class RelevanceContext:
    """The real assembler: a budget, memory chosen for this turn, and the tail of
    the conversation that fits in what is left."""

    def __init__(self, *, budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                 memory_share: float = MEMORY_SHARE) -> None:
        self.budget_tokens = budget_tokens
        self.memory_share = memory_share

    def assemble(self, *, session_id: str, text: str, low_confidence: bool = False,
                 ) -> AssembledContext:
        memory_budget = int(self.budget_tokens * self.memory_share)

        conflicted = memory_store.conflicted_memory_ids()
        available = [m for m in memory_store.list_memories() if m["id"] not in conflicted]
        chosen, selection = select_memories(text, available, memory_budget)
        memories_text = memory_store.approved_memories_text(chosen)

        system = prompt.system_instruction(memories=memories_text,
                                           low_confidence=low_confidence)
        remaining = max(0, self.budget_tokens - estimate_tokens(system))
        messages = trim_messages(conversation.get_messages(session_id), remaining)

        included = ("system_instruction",)
        if chosen:
            included += ("memory",)
        if messages:
            included += ("conversation",)

        return AssembledContext(
            system=system,
            messages=messages,
            included=included,
            notes={
                "budgetTokens": self.budget_tokens,
                "estimatedTokens": estimate_tokens(system) + sum(
                    estimate_tokens(str(m.get("text") or "")) for m in messages),
                "estimate": "characters / 4 — an estimate, not a tokeniser",
                "memory": selection,
                "messagesKept": len(messages),
                "messagesAvailable": len(conversation.get_messages(session_id)),
            },
        )


class WindowContext:
    """The recent conversation window and a fixed system prompt, with no relevance
    selection at all — kept for tests and for a caller that genuinely wants no
    memory in the prompt. Named for what it does."""

    def __init__(self, system: str = "") -> None:
        self._system = system

    def assemble(self, *, session_id: str, text: str, low_confidence: bool = False,
                 ) -> AssembledContext:
        return AssembledContext(
            system=self._system,
            messages=conversation.get_messages(session_id),
            included=("conversation_window",),
            notes={"strategy": "recent window only, no relevance selection"},
        )
