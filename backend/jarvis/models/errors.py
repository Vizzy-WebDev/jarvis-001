"""The canonical errors. Every driver maps its own status codes and error bodies to
these — including errors that arrive mid-stream and errors inside a successful
response — so no status code appears anywhere outside the drivers.

`str(error)` is written for a person, in plain words: never the provider's own text or
a status code. Those go in `detail` (`provider_words`, `status`) for the trace.
`retryable` is the only thing the executor reads: a retryable error is retried and
then falls back; any other error ends the call.
"""

from __future__ import annotations

from typing import Any, ClassVar


class ModelError(RuntimeError):
    type: ClassVar[str] = "error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, endpoint_id: str | None = None,
                 retry_after: float | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.endpoint_id = endpoint_id
        #: Seconds the server asked us to wait, when it said.
        self.retry_after = retry_after
        self.detail = detail or {}

    def at(self, endpoint_id: str) -> "ModelError":
        if self.endpoint_id is None:
            self.endpoint_id = endpoint_id
        return self


class RateLimited(ModelError):
    type = "rate_limited"
    retryable = True


class Unavailable(ModelError):
    type = "unavailable"
    retryable = True


class Timeout(ModelError):
    type = "timeout"
    retryable = True


class Auth(ModelError):
    """The key was refused — and also a provider's billing or quota refusal."""

    type = "auth"


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


class SchemaValidationFailed(ModelError):
    type = "schema_validation_failed"


ALL = (RateLimited, Unavailable, Timeout, Auth, InvalidRequest, ContextTooLong, ContentRefused,
       NoEligibleEndpoint, BudgetExceeded, SchemaValidationFailed)
BY_TYPE = {cls.type: cls for cls in ALL}
