"""The canonical errors. Every driver maps its own status codes and error bodies to
these — including errors that arrive mid-stream and errors inside a successful
response — so no status code appears anywhere outside the drivers.

`str(error)` is written for a person, in plain words: never the provider's own text or
a status code. Those go in `detail` (`provider_words`, `status`) for the trace.
Every error also says how far it reaches (`scope`: the request alone, one endpoint, one
upstream behind a gateway, or the whole connection) and how it heals (`recovery`: after a
wait, at the provider's reset, on a refresh of the model list, or only when the person
acts). Drivers and quirk data set these; the rest of the layer only reads them.
One rule follows: a failure whose scope is wider than the request retries or falls back
under Auto along the filtered list, before the first streamed event; a request-scoped one
ends the call; a pin always ends it (see execute.py).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

Scope = Literal["request", "endpoint", "upstream", "connection"]
#: after = a wait (`retry_after`, else a default); reset = the provider's own reset time;
#: refresh = until the model list is next fetched; user = until the person edits the key,
#: tests or reconnects; none = nothing to heal (the request itself was the problem).
Recovery = Literal["after", "reset", "refresh", "user", "none"]
SCOPES = ("request", "endpoint", "upstream", "connection")


class ModelError(RuntimeError):
    type: ClassVar[str] = "error"
    retryable: ClassVar[bool] = False
    scope: ClassVar[Scope] = "request"
    recovery: ClassVar[Recovery] = "none"

    def __init__(self, message: str, *, endpoint_id: str | None = None,
                 retry_after: float | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.endpoint_id = endpoint_id
        #: Seconds the server asked us to wait, when it said.
        self.retry_after = retry_after
        self.detail = detail or {}
        #: A driver may widen or narrow what its class says (a daily limit heals at a reset).
        self.scope: Scope = type(self).scope
        self.recovery: Recovery = type(self).recovery

    def with_scope(self, scope: Scope, recovery: Recovery | None = None) -> "ModelError":
        self.scope = scope
        if recovery is not None:
            self.recovery = recovery
        return self

    @property
    def widens(self) -> bool:
        """True when the failure is about more than this one request."""
        return self.scope != "request"

    def at(self, endpoint_id: str) -> "ModelError":
        if self.endpoint_id is None:
            self.endpoint_id = endpoint_id
        return self


class RateLimited(ModelError):
    type = "rate_limited"
    retryable = True
    scope = "endpoint"
    recovery = "after"


class Unavailable(ModelError):
    type = "unavailable"
    retryable = True
    scope = "endpoint"
    recovery = "after"


class Timeout(ModelError):
    type = "timeout"
    retryable = True
    scope = "endpoint"
    recovery = "after"


class Auth(ModelError):
    """The key was refused."""

    type = "auth"
    scope = "connection"
    recovery = "user"


class Billing(ModelError):
    """The account is out of credit or over its quota: nothing on this connection will work
    until the person acts, and retrying cannot help."""

    type = "billing"
    scope = "connection"
    recovery = "user"


class ModelNotFound(ModelError):
    """The server says it has no such model. Other models on it are unaffected."""

    type = "model_not_found"
    scope = "endpoint"
    recovery = "refresh"


class InvalidRequest(ModelError):
    type = "invalid_request"


class ContextTooLong(ModelError):
    type = "context_too_long"


class ContentRefused(ModelError):
    type = "content_refused"


class NoEligibleEndpoint(ModelError):
    type = "no_eligible_endpoint"

    def __init__(self, message: str, *, rejections: tuple[Any, ...] = (), **kw: Any) -> None:
        super().__init__(message, **kw)
        self.rejections = rejections


class BudgetExceeded(ModelError):
    type = "budget_exceeded"

    def __init__(self, message: str, *, rejections: tuple[Any, ...] = (), **kw: Any) -> None:
        super().__init__(message, **kw)
        self.rejections = rejections


class SchemaValidationFailed(ModelError):
    type = "schema_validation_failed"


ALL = (RateLimited, Unavailable, Timeout, Auth, Billing, ModelNotFound, InvalidRequest, ContextTooLong, ContentRefused,
       NoEligibleEndpoint, BudgetExceeded, SchemaValidationFailed)
BY_TYPE = {cls.type: cls for cls in ALL}
