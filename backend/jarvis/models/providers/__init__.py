"""One module per wire format, each with the same three functions:

    check(target)                      -> CheckResult   is the connection good?
    discover(target)                   -> [Discovered]  what does it offer? (may raise Unsupported)
    stream(target, *, model_id, messages, system, tools, effort, facts) -> events

That is the whole shared interface. There is no base class and nothing to
register: a format is a module, and this is the dictionary that names them.
"""

from __future__ import annotations

from types import ModuleType

from ..errors import ProviderError
from . import anthropic_messages, gemini_generate, openai_chat, openai_responses

_BY_FORMAT: dict[str, ModuleType] = {
    m.FORMAT: m for m in (openai_responses, openai_chat, anthropic_messages, gemini_generate)
}


def for_format(format_id: str) -> ModuleType:
    try:
        return _BY_FORMAT[format_id]
    except KeyError:
        raise ProviderError(f"Jarvis doesn't know how to talk to a provider of type “{format_id}”.",
                            kind="request") from None
