"""What can go wrong talking to a provider, said the way a person can read it."""

from __future__ import annotations

#: How far a failure reaches, as the ADAPTER that read the provider's answer judged it.
#: The router acts on this and never works it out for itself:
#:
#: * ``request``    — this request is wrong wherever it goes (malformed, refused as
#:   invalid). Nothing else is tried and nothing is held back.
#: * ``model``      — this model, reached with this credential, can't answer right now.
#: * ``credential`` — the key/account behind the connection is refused or out of credit.
#: * ``provider``   — the service at that address is down, overloaded or unreachable.
#: * ``unknown``    — the provider's answer said nothing either way; treated like ``model``.
SCOPES = ("request", "model", "credential", "provider", "unknown")


class ProviderError(RuntimeError):
    """A provider refused, failed, or could not be reached.

    `str(error)` is written for the person, and always carries the provider's own
    words where it gave any: paraphrasing a provider's refusal is how a real
    reason ("that model needs a paid plan") turns into a vague one.

    Everything else is for code that needs to react, never for display:

    * `kind` — what happened: 'auth' | 'forbidden' | 'billing' | 'model' | 'rate'
      | 'unreachable' | 'network' | 'server' | 'request' | 'unsupported' | 'reply'
      | 'overloaded'.
    * `scope` — how far it reaches (see `SCOPES`). Set by the provider module that
      read the answer, from the provider's own error body where it gave one.
    * `retryable_elsewhere` — a later attempt could succeed: this model again after a
      pause, or another candidate. False for failures that would only repeat (a
      refused key, a bad request, a model that doesn't exist).
    * `retry_after_s` — how long the PROVIDER said to wait, when it said.
    * `status` — the HTTP status, when there was one.
    """

    def __init__(self, message: str, *, kind: str = "request", scope: str = "unknown",
                 retryable_elsewhere: bool = False, retry_after_s: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        if scope not in SCOPES:
            raise ValueError(f"unknown error scope {scope!r}")
        self.kind = kind
        self.scope = scope
        self.retryable_elsewhere = retryable_elsewhere
        self.retry_after_s = retry_after_s
        self.status = status


class Unsupported(ProviderError):
    """This provider does not offer what was asked — today only a model list.
    Distinct from a failure: it is a fact about the provider, not a problem."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="unsupported", scope="provider")
