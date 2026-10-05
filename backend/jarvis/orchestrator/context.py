"""Assembling what the model actually sees, under a stated budget (§23).

§23 asks for relevance-based assembly — recent conversation, relevant long-term
memory, the current task, tool results, and a token budget — not "put everything
in the prompt". The original injects the ENTIRE approved memory set into every
call, which works only for as long as the set stays small, and stops being true
exactly when memory starts being worth having.

**Four decisions worth stating, because each trades something real:**

1. **A small memory goes in whole; a larger one is searched — by meaning when it can be.**
   Up to `SMALL_MEMORY` memories cost little and every one might matter, so all of them go in
   and no model is asked anything. Above that, relevance is keyword overlap fused (reciprocal
   rank) with similarity of meaning from an embedding model (`memory/vectors.py`), so "what
   should I cook tonight?" finds "Vegetarian — never suggest meat" though they share no word.
   Only a memory whose similarity stands clearly above the rest counts as close in meaning —
   a fixed cosine threshold means something different for every model. Searching by meaning is
   never required: no embedding model, one not answering in time, a privacy setting against it,
   or a message too short to mean anything all fall back to keywords, and `selection` on the
   returned context says exactly what went in and why — including which way it was found.

2. **Importance overrides relevance, on purpose.** A fact the user marked as
   changing how they should be helped must not drop out because the current
   sentence happens to share no words with it. Relevance decides what ELSE gets
   in, never what gets excluded from the floor.

3. **The budget is the answering model's, never a number chosen here.** The turn
   loop asks the model layer which model will take the turn and what it can hold
   (`budget_for` turns that into room for this assembly). A model that has never
   said how much it can hold gets no limit from Jarvis at all: everything relevant
   goes in, and if that model refuses the request as too long, the size it refused
   is recorded as what it can take and the step is assembled again to fit.
   Counting is in estimated tokens (characters / 4), and the estimate is labelled
   as one.

4. **What gives way first, when the budget is tight:** older history before the
   current turn (whose own content is a floor), and within that history, earlier
   turns' long tool results are shortened before any whole turn is dropped. What
   is older than the running summary (`conversation_summary.py`) is represented by
   the summary rather than repeated.

Cutting the transcript is done with the same rule the conversation store uses: a
slice must never land between an assistant's tool call and its result, because
every provider rejects a transcript with an orphaned call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .. import conversation, conversation_summary, prompt
from ..memory import store as memory_store
from ..memory import vectors as memory_vectors
from ..open_work import snapshot as open_work_snapshot

logger = logging.getLogger(__name__)

#: What memory may take of the model's budget. Everything else goes to the
#: transcript, which is what the user is actually talking about right now.
MEMORY_SHARE = 0.2

#: Of a model's own window: what is kept back for its reply when it declares no
#: maximum output (and the most a declared maximum may claim), and a margin for
#: the difference between any estimate and the provider's real tokeniser. Both are
#: shares of the model's own size, so they scale with it.
REPLY_SHARE = 0.25
SAFETY_SHARE = 0.05

#: How the model layer counts a request (`models/resolve.py estimate_tokens`):
#: characters / 3, plus 10%. Converting from its units to this module's is the one
#: place the two rules meet, so Jarvis's own assembly never trips the layer's
#: "too big for this model" check.
_LAYER_CHARS_PER_TOKEN = 3
_LAYER_OVERHEAD = 1.1
_CHARS_PER_TOKEN = 4

#: An earlier turn's tool result, once the budget is tight, keeps this much of its
#: text. A stub length, not a context limit: it only ever applies when the
#: alternative is dropping that whole turn.
SHORTENED_RESULT_CHARS = 600


def budget_for(context_tokens: int | None, output_tokens: int | None = None,
               tool_chars: int = 0) -> int | None:
    """Room for this assembly, in estimated tokens, from what the answering model
    says it can hold — or None when it has never said, meaning no limit."""
    if not context_tokens or context_tokens <= 0:
        return None
    reply_cap = context_tokens * REPLY_SHARE
    reply = min(output_tokens, reply_cap) if output_tokens else reply_cap
    usable_layer_tokens = context_tokens - reply - context_tokens * SAFETY_SHARE
    usable_chars = usable_layer_tokens / _LAYER_OVERHEAD * _LAYER_CHARS_PER_TOKEN - tool_chars
    return max(0, int(usable_chars // _CHARS_PER_TOKEN))

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


@dataclass(frozen=True)
class AgentBrief:
    """Who a turn is being run AS, when it is a specialist's rather than Jarvis's.

    Plain data, built by `agents/runner.py` and carried on `TurnRequest.agent`, so
    neither this module nor the turn loop ever imports the agents package: the
    assembler only needs the words, never where they came from.
    """

    agent_id: str
    name: str
    mission: str = ""
    doctrine: str = ""
    #: The agent's own guardrails with the ones every specialist shares already
    #: joined on — composed once by the runner.
    guardrails: str = ""
    #: Whether the user's saved Memory goes into this agent's prompt.
    memory: bool = True
    #: Who this agent may ask for help, as (id, name, what they do). Empty when it
    #: may ask nobody.
    collaborators: tuple[tuple[str, str, str], ...] = ()
    #: True when the operator is talking to this agent directly in the chat, rather
    #: than through Jarvis — then its reply IS what they read.
    direct: bool = False


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


#: What a specialist agent always sees of the user's Memory, whatever its task says:
#: what they are working towards. An opportunity hunt or a strategy has to start
#: from their goals even when the task's own words share nothing with them.
SPECIALIST_FLOOR_CATEGORIES = ("Long-term Goals", "Projects")


#: At or below this many memories, there is no choosing to do: everything goes in. A relevance
#: heuristic about a SMALL curated list, not a context limit — what the model can hold is still
#: the budget's say (`budget_tokens`), and a memory set this small costs a few hundred tokens. It
#: also means a reworded question can never miss a fact while memory is small, with no embedding
#: call and no delay.
SMALL_MEMORY = 20

#: Above that, at most this many memories chosen by relevance ride beside the always-included
#: ones. Likewise a relevance cap, not a context size: twelve facts relevant to a message is
#: plenty, and the 13th is noise however large the window is.
MAX_RELEVANT = 12

#: How far above the crowd a memory's similarity must stand to count as relevant by meaning: this many
#: standard deviations over the mean of THIS question's similarities. Cosine values mean different
#: things for different embedding models (an unrelated pair scores 0.1 on one and 0.4 on another), so
#: a fixed cut-off would be wrong for most of them; "stands out from the rest of the memory" is true
#: of all. Needs enough memories to have a crowd (`MIN_FOR_SPREAD`); below that nothing is cut.
VECTOR_STANDOUT = 1.5
MIN_FOR_SPREAD = 8

#: Reciprocal-rank fusion's constant: how fast a lower rank stops mattering. 60 is the standard
#: value; it is scale-free, so keyword scores and cosine similarities never need to be made
#: comparable to each other.
RRF_K = 60


def _ranked(scored: list[tuple[float, int, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [m for _, _, m in sorted(scored, key=lambda row: (-row[0], row[1]))]


def select_memories(text: str, memories: list[dict[str, Any]], budget_tokens: int | None,
                    *, floor_categories: tuple[str, ...] = (),
                    similarity: dict[str, float] | None = None,
                    why_not: str = "",
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The memories worth this turn's budget, and an account of the choice.
    `budget_tokens=None` (the model never said its size) caps nothing but relevance.

    - **Small memory** (`SMALL_MEMORY` or fewer): all of it, floor first.
    - **Larger memory:** the floor, then the most relevant — by keyword overlap fused (reciprocal
      rank) with similarity of meaning when `similarity` says how close each memory is to what
      was said, and by keyword overlap alone when it is None (`why_not` says why, in `strategy`).
      A memory with no similarity of its own is ranked by keyword alone, never dropped for it.
    """
    terms = _terms(text)
    keyword = [(score_memory(m, terms, i), i, m) for i, m in enumerate(memories)]
    overlapping = [(score, i, m) for score, i, m in keyword if score > 0]

    floor = [m for m in memories if (m.get("importance") or 0) >= ALWAYS_INCLUDE_IMPORTANCE
             or m.get("category") in floor_categories]
    floor_ids = {m["id"] for m in floor}

    if len(memories) <= SMALL_MEMORY:
        strategy = f"everything (memory is small: {len(memories)} of up to {SMALL_MEMORY})"
        relevant = [m for m in _ranked(keyword) if m["id"] not in floor_ids]
    else:
        # In a search by meaning only a REAL word match counts as a keyword hit: importance lifts a
        # memory's score on the keyword-only path, but it is not relevance and must not pose as it.
        shared = [row for row in keyword if _terms(row[2].get("text", "")) & terms]
        keyword_rank = {m["id"]: r for r, m in enumerate(_ranked(shared))}
        vector_rank: dict[str, int] = {}
        if similarity:
            offered = {m["id"] for m in memories}
            values = [v for k, v in similarity.items() if k in offered]
            bar = float("-inf")
            if len(values) >= MIN_FOR_SPREAD:
                mean = sum(values) / len(values)
                spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
                # No spread at all (every memory equally close) means nothing stands out.
                bar = mean + VECTOR_STANDOUT * spread if spread > 0 else float("inf")
            ordered = sorted((m for m in memories if m["id"] in similarity
                              and similarity[m["id"]] >= bar),
                             key=lambda m: (-similarity[m["id"]], -(m.get("importance") or 0)))
            vector_rank = {m["id"]: r for r, m in enumerate(ordered)}

        def fused(row: tuple[float, int, dict[str, Any]]) -> float:
            memory_id = row[2]["id"]
            total = 0.0
            if memory_id in keyword_rank:
                total += 1.0 / (RRF_K + keyword_rank[memory_id])
            if memory_id in vector_rank:
                total += 1.0 / (RRF_K + vector_rank[memory_id])
            # Importance only breaks ties (it is far smaller than one rank step).
            return total + (row[2].get("importance") or 0) * 1e-6

        if similarity:
            strategy = "meaning (embeddings) fused with keyword overlap, plus importance"
            pool = [row for row in keyword if row[2]["id"] not in floor_ids
                    and (row[2]["id"] in keyword_rank or row[2]["id"] in vector_rank)]
            relevant = _ranked([(fused(row), row[1], row[2]) for row in pool])[:MAX_RELEVANT]
        else:
            strategy = ("keyword overlap + importance + recency"
                        + (f" ({why_not})" if why_not else ""))
            relevant = [m for m in _ranked(overlapping) if m["id"] not in floor_ids][:MAX_RELEVANT]

    chosen: list[dict[str, Any]] = []
    used = 0
    for memory in floor + relevant:
        cost = estimate_tokens(memory.get("text", "")) + 8  # the "- ... (noted ...)" wrapper
        if budget_tokens is not None and used + cost > budget_tokens:
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
        "strategy": strategy,
    }


def message_cost(message: dict[str, Any]) -> int:
    return (estimate_tokens(str(message.get("text") or ""))
            + estimate_tokens(str(message.get("toolCalls") or ""))
            + estimate_tokens(str(message.get("toolResults") or "")))


def trim_messages(messages: list[dict[str, Any]], budget_tokens: int) -> list[dict[str, Any]]:
    """The newest messages that fit, never cutting a tool call from its result —
    and never dropping the turn being answered.

    The current turn (the person's latest message and everything since) is a floor,
    kept whatever the budget says. Found live: a long result carried in the system
    prompt left the transcript a budget of zero, the person's own question was cut,
    and the model answered a message it never saw ("what would you like to tackle
    today?"). Older history is what gives way, not the question.
    """
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    floor = messages[last_user:] if last_user is not None else []
    older = messages[:last_user] if last_user is not None else list(messages)

    used = sum(message_cost(m) for m in floor)
    kept: list[dict[str, Any]] = []
    for message in reversed(older):
        cost = message_cost(message)
        if used + cost > budget_tokens and (kept or floor):
            break
        kept.append(message)
        used += cost
    kept.reverse()
    kept += floor

    # A transcript that STARTS with tool results has lost the assistant message
    # that requested them; every provider rejects that outright.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept


def shortened_result(result: Any) -> Any:
    import json

    try:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) <= SHORTENED_RESULT_CHARS:
        return result
    return text[:SHORTENED_RESULT_CHARS] + " …(shortened — shown in full earlier in the conversation)"


def shorten_old_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same transcript with every tool result BEFORE the latest user message cut to
    a short excerpt. Copies — the stored conversation is never touched — and the current
    turn's own results are left whole, since that is what is being worked on."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    if last_user is None:
        return list(messages)
    out = []
    for index, message in enumerate(messages):
        if index < last_user and message.get("role") == "tool" and message.get("toolResults"):
            message = {**message, "toolResults": [{**entry, "result": shortened_result(entry.get("result"))}
                                                  for entry in message["toolResults"]]}
        out.append(message)
    return out


def fit_messages(messages: list[dict[str, Any]], budget_tokens: int | None) -> list[dict[str, Any]]:
    """The transcript to send, within the model's budget.

    No budget: everything. Everything fits: everything, unchanged. Otherwise older
    turns' tool results are shortened first, and only then are whole older turns
    dropped (oldest first) — so one big web result costs its own detail, not the
    conversation around it.
    """
    if budget_tokens is None or sum(message_cost(m) for m in messages) <= budget_tokens:
        return list(messages)
    return trim_messages(shorten_old_tool_results(messages), budget_tokens)


def _waiting_notices() -> list[dict[str, Any]]:
    """Undelivered rows worth mentioning.

    A READ, not a recorder — the same distinction that lets this module read
    memory to build a prompt while the turn loop itself may not reach a recorder
    at all. A failure here returns nothing: a turn must never fail because of a
    notice.
    """
    from ..heartbeat import outbox

    try:
        return outbox.list_pending(max_tier=2)
    except Exception:  # noqa: BLE001
        logger.exception("could not read waiting notices")
        return []


def _answered_approvals(session_id: str) -> str:
    """What the person answered, since the last real reply, about actions this
    session asked to take — and what each approved one returned.

    The answer arrives in its own request, after the asking turn has ended, so
    without this the next turn cannot know it happened (found live: a specialist
    told "approved, carry on" asked to run the same code again). A READ, and a
    failure here returns nothing: a turn must never fail because of it.
    """
    from ..policy import approvals

    try:
        messages = conversation.get_messages(session_id)
        replies = [m for m in messages if m.get("role") == "assistant" and m.get("text")
                   and not m.get("toolCalls")]
        since = str(replies[-1].get("createdAt") or "") if replies else ""
        return prompt.answered_approvals_section(approvals.answered_since(session_id, since))
    except Exception:  # noqa: BLE001
        logger.exception("could not read answered approvals")
        return ""


def _history(session_id: str) -> tuple[list[dict[str, Any]], str, int | None]:
    """The conversation as it is sent: the running summary (rendered) and the messages it
    does not already cover. Unbound sessions (jobs, scheduled runs) have no summary."""
    messages = conversation.get_messages(session_id)
    summary = conversation_summary.get(session_id)
    if summary is None:
        return messages, "", None
    covered = summary["coveredSeq"]
    newer = [m for m in messages if not isinstance(m.get("seq"), int) or m["seq"] > covered]
    return newer, conversation_summary.render(summary["summary"]), covered


class RelevanceContext:
    """The real assembler: memory chosen for this turn, the running summary of what
    scrolled out, and the tail of the conversation — within whatever the answering
    model can hold (`budget_tokens`, from `budget_for`), or everything relevant when
    the model has never said."""

    def __init__(self, *, memory_share: float = MEMORY_SHARE) -> None:
        self.memory_share = memory_share

    def assemble(self, *, session_id: str, text: str, low_confidence: bool = False,
                 background: bool = False, agent: AgentBrief | None = None,
                 budget_tokens: int | None = None) -> AssembledContext:
        memory_budget = int(budget_tokens * self.memory_share) if budget_tokens is not None else None

        conflicted = memory_store.conflicted_memory_ids()
        available = [m for m in memory_store.list_memories() if m["id"] not in conflicted]
        if agent is not None and not agent.memory:
            # The person decided this agent does not see what Jarvis knows about them.
            available = []
        similarity, why_not = None, ""
        if len(available) > SMALL_MEMORY:
            # Only a LARGE memory needs searching, so a small one never pays for an embedding
            # call. Never raises, never waits long; None means "use keywords" (`memory/vectors.py`).
            similarity, why_not = memory_vectors.similarities(text, available)
        chosen, selection = select_memories(
            text, available, memory_budget,
            floor_categories=SPECIALIST_FLOOR_CATEGORIES if agent is not None else (),
            similarity=similarity, why_not=why_not)
        memories_text = memory_store.approved_memories_text(chosen)
        history, summary_text, covered = _history(session_id)
        so_far = prompt.conversation_so_far_section(summary_text)

        if agent is not None:
            return self._specialist(agent, session_id=session_id, memories_text=memories_text,
                                    chosen=chosen, selection=selection,
                                    low_confidence=low_confidence, budget_tokens=budget_tokens,
                                    history=history, so_far=so_far, covered=covered)

        # Rules Jarvis has learned about its own work ride in the volatile half:
        # they change as it learns, and they apply to a background job's turn as
        # much as to a conversation.
        from ..improvement.store import active_rules_text

        rules = prompt.rules_section(active_rules_text())

        # Things waiting for the user — only on a turn a PERSON started. A
        # background job's own turn has nobody to tell, and putting a notice
        # there would deliver it to the machinery instead of to them.
        notices = "" if background else prompt.notices_section(_waiting_notices(),
                                                                session_id=session_id)
        # What Jarvis has going for them, so "still on it" is something it knows. Same gate:
        # a background turn has nobody to say it to.
        open_work = "" if background else prompt.open_work_section(open_work_snapshot())

        # The adaptive communication register — both its stable half
        # (STYLE_FRAMEWORK, via has_audience below) and its per-turn half
        # (the floors) share one gate: `has_audience = not background`. A briefing's own
        # turn also runs with `background=True` (there is no separate `addressed` flag
        # threaded through `TurnRequest`/`Surface`), so a briefing gets neither half.
        has_audience = not background
        floors_text = ""
        if has_audience:
            from ..personality import floors_section, read_style

            floors, sticky = read_style(session_id, text)
            floors_text = floors_section(floors, sticky)

        answered = _answered_approvals(session_id)
        system = prompt.system_instruction(
            memories=memories_text, low_confidence=low_confidence,
            extra=[p for p in (("conversation_so_far", so_far), ("open_work", open_work),
                               ("learned_rules", rules),
                               ("answered_approvals", answered), ("waiting_notices", notices),
                               ("style_now", floors_text)) if p[1]],
            has_audience=has_audience)
        remaining = (max(0, budget_tokens - estimate_tokens(system))
                     if budget_tokens is not None else None)
        messages = fit_messages(history, remaining)

        included = ("system_instruction",)
        if so_far:
            included += ("conversation_so_far",)
        if rules:
            included += ("learned_rules",)
        if notices:
            included += ("waiting_notices",)
        if chosen:
            included += ("memory",)
        if messages:
            included += ("conversation",)

        return AssembledContext(
            system=system,
            messages=messages,
            included=included,
            notes={
                "budgetTokens": budget_tokens,
                "estimatedTokens": estimate_tokens(system) + sum(message_cost(m) for m in messages),
                "estimate": "characters / 4 — an estimate, not a tokeniser",
                "memory": selection,
                "messagesKept": len(messages),
                "messagesAvailable": len(history),
                "summaryCoveredSeq": covered,
            },
        )

    def _specialist(self, agent: AgentBrief, *, session_id: str, memories_text: str,
                    chosen: list[dict[str, Any]], selection: dict[str, Any],
                    low_confidence: bool, budget_tokens: int | None,
                    history: list[dict[str, Any]], so_far: str,
                    covered: int | None) -> AssembledContext:
        """A specialist's own prompt: its identity and doctrine in place of Jarvis's,
        the same honesty rules, and the same learned rules. No waiting notices —
        those are delivered by Jarvis to the person, never to a specialist."""
        from ..improvement.store import active_rules_text

        rules = prompt.rules_section(active_rules_text())
        answered = _answered_approvals(session_id)
        system = prompt.specialist_instruction(
            agent, memories=memories_text, low_confidence=low_confidence,
            extra=[p for p in (("conversation_so_far", so_far), ("learned_rules", rules),
                               ("answered_approvals", answered)) if p[1]] or None)
        remaining = (max(0, budget_tokens - estimate_tokens(system))
                     if budget_tokens is not None else None)
        messages = fit_messages(history, remaining)
        included = ("specialist_instruction",)
        if so_far:
            included += ("conversation_so_far",)
        if rules:
            included += ("learned_rules",)
        if chosen:
            included += ("memory",)
        if messages:
            included += ("conversation",)
        return AssembledContext(
            system=system, messages=messages, included=included,
            notes={"agent": agent.agent_id, "budgetTokens": budget_tokens,
                   "memory": selection, "messagesKept": len(messages),
                   "summaryCoveredSeq": covered})


class WindowContext:
    """The recent conversation window and a fixed system prompt, with no relevance
    selection at all — kept for tests and for a caller that genuinely wants no
    memory in the prompt. Named for what it does."""

    def __init__(self, system: str = "") -> None:
        self._system = system

    def assemble(self, *, session_id: str, text: str, low_confidence: bool = False,
                 **_: Any) -> AssembledContext:
        return AssembledContext(
            system=self._system,
            messages=conversation.get_messages(session_id),
            included=("conversation_window",),
            notes={"strategy": "recent window only, no relevance selection"},
        )
