"""What can go wrong talking to a provider, said the way a person can read it."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """A provider refused, failed, or could not be reached.

    `str(error)` is written for the person, and always carries the provider's own
    words where it gave any: paraphrasing a provider's refusal is how a real
    reason ("that model needs a paid plan") turns into a vague one.

    `kind` is for code that needs to react, never for display:
    'auth' | 'forbidden' | 'billing' | 'model' | 'rate' | 'unreachable' | 'network'
    | 'server' | 'request' | 'unsupported' | 'reply'.
    'auth' and 'unreachable' are about the whole connection, not one model.
    """

    def __init__(self, message: str, *, kind: str = "request", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class Unsupported(ProviderError):
    """This provider does not offer what was asked — today only a model list.
    Distinct from a failure: it is a fact about the provider, not a problem."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="unsupported")
