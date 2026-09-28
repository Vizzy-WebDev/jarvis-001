"""One module per wire format, each with the same four functions and a `FORMAT`:

    check(target)                         -> CheckResult     is the connection good?
    discover(target)                      -> [Discovered]    what does it offer? (may raise Unsupported)
    stream(target, request, *, facts)     -> events          text as it comes, then one Finished
    normalize_error(raw)                  -> ProviderError   what its failure means, and how far it reaches

That is the whole shared interface, written down as `models.adapter.ProviderAdapter`
and asserted by `tests/conformance/`. There is no base class and nothing to register:
a format is a module, and this is the dictionary that names them.
"""

from __future__ import annotations

from typing import cast

from ..adapter import ProviderAdapter
from ..errors import ProviderError
from . import anthropic_messages, gemini_generate, openai_chat, openai_responses

_BY_FORMAT: dict[str, ProviderAdapter] = {
    m.FORMAT: cast(ProviderAdapter, m) for m in (openai_responses, openai_chat, anthropic_messages, gemini_generate)
}


def for_format(format_id: str) -> ProviderAdapter:
    try:
        return _BY_FORMAT[format_id]
    except KeyError:
        raise ProviderError(f"Jarvis doesn't know how to talk to a provider of type “{format_id}”.",
                            kind="request", scope="provider") from None
