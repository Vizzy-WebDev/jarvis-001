"""An HTTP API as a set of callable tools.

Operations come from an OpenAPI document when the service publishes one, or are
added by hand one at a time. Either way they end up in the same shape, so
calling one never has to know where it came from.

**The key never reaches the model.** It is stored as a secret reference and read
at call time; a declaration carries the operation's name, description and
parameters and nothing else.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
BODY_METHODS = ("post", "put", "patch")
MAX_RESPONSE_CHARS = 20000
TIMEOUT_S = 30.0

_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


def sanitise_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", str(name or "")).strip("_").lower()
    return (cleaned or "operation")[:60]


def discover_from_spec(spec_url: str, *, fetch: Any = None) -> dict[str, Any]:
    """Read an OpenAPI/Swagger document into operations.

    Both live shapes are handled, because both are still common: OpenAPI 3's
    `servers[0].url` — often relative, so resolved against the document's own
    address — and Swagger 2's `host`/`basePath`/`schemes`. Normalised to one
    absolute base URL here, so calling an operation never has to care which.
    """
    if fetch is None:
        import httpx

        def fetch(url: str) -> Any:  # noqa: ANN401
            with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
                return client.get(url)

    try:
        response = fetch(spec_url)
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"Couldn't reach that address: {err}") from err
    if getattr(response, "status_code", 200) >= 400:
        raise ValueError(f"That address responded with an error ({response.status_code}).")

    try:
        spec = response.json()
    except Exception as err:  # noqa: BLE001
        raise ValueError(
            "That doesn't look like a JSON OpenAPI document — a YAML spec isn't supported. "
            'Look for an "/openapi.json" address for this service, or add endpoints by hand.'
        ) from err

    if not isinstance(spec.get("paths"), dict):
        raise ValueError('That document has no OpenAPI "paths" section — is it really a spec?')

    servers = spec.get("servers") or []
    base_url = None
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        base_url = urljoin(spec_url, servers[0]["url"])
    elif spec.get("host"):
        scheme = (spec.get("schemes") or ["https"])[0]
        base_url = f"{scheme}://{spec['host']}{spec.get('basePath') or ''}"

    operations = []
    for path_template, methods in spec["paths"].items():
        if not isinstance(methods, dict):
            continue
        for method in HTTP_METHODS:
            operation = methods.get(method)
            if not isinstance(operation, dict):
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            for parameter in operation.get("parameters") or []:
                if not isinstance(parameter, dict) or not parameter.get("name"):
                    continue
                described = {"type": "string"}
                if parameter.get("description"):
                    described["description"] = str(parameter["description"])
                properties[parameter["name"]] = described
                if parameter.get("required"):
                    required.append(parameter["name"])
            if method in BODY_METHODS and operation.get("requestBody"):
                # A full schema translation of an arbitrary request body is real
                # work with little payoff: one generic object, which the model
                # fills in from the operation's own description, covers the
                # common case without claiming a precision this does not have.
                properties["body"] = {"type": "object",
                                      "description": "The request body, as JSON."}
            operations.append({
                "name": sanitise_name(operation.get("operationId")
                                      or f"{method}_{path_template}"),
                "description": (operation.get("summary") or operation.get("description")
                                or f"{method.upper()} {path_template}"),
                "method": method.upper(),
                "path": path_template,
                "parameters": {"type": "object", "properties": properties,
                               "required": required},
            })

    if not operations:
        raise ValueError("No usable endpoints were found in that document.")
    return {"baseUrl": base_url, "operations": operations}


def tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": op["name"], "description": op.get("description") or op["name"],
             "parameters": op.get("parameters") or {"type": "object", "properties": {}}}
            for op in (config or {}).get("operations") or []
            if op.get("enabled", True)]


def _authorise(config: dict[str, Any], headers: dict[str, str],
               params: dict[str, Any]) -> None:
    from ..config import get_secret

    auth = (config or {}).get("auth") or {}
    secret_ref = config.get("secretRef")
    key = get_secret(secret_ref) if secret_ref else None
    if not key:
        return
    kind = auth.get("kind") or "bearer"
    if kind == "bearer":
        headers["Authorization"] = f"{auth.get('prefix') or 'Bearer'} {key}"
    elif kind == "header":
        headers[auth.get("name") or "X-API-Key"] = key
    elif kind == "query":
        params[auth.get("name") or "api_key"] = key


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any], *,
             request: Any = None) -> dict[str, Any]:
    operation = next((o for o in (config or {}).get("operations") or []
                      if o.get("name") == name), None)
    if operation is None:
        raise KeyError(f"Unknown operation: {name}")
    base_url = (config or {}).get("baseUrl")
    if not base_url:
        raise ValueError("That connector has no address to call.")

    path = operation["path"]
    remaining = dict(args or {})
    body = remaining.pop("body", None)
    # Path placeholders are filled from the arguments and removed, so what is
    # left is a query string rather than being sent twice.
    for key in list(remaining):
        placeholder = "{" + key + "}"
        if placeholder in path:
            path = path.replace(placeholder, str(remaining.pop(key)))

    headers: dict[str, str] = {"Accept": "application/json"}
    params: dict[str, Any] = {}
    _authorise(config, headers, params)
    params.update(remaining)

    url = base_url.rstrip("/") + "/" + path.lstrip("/")

    if request is None:
        import httpx

        def request(**kw: Any) -> Any:  # noqa: ANN401
            with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
                return client.request(**kw)

    response = request(method=operation["method"], url=url, headers=headers,
                       params=params, json=body if body is not None else None)

    text = getattr(response, "text", "") or ""
    truncated = len(text) > MAX_RESPONSE_CHARS
    answer: dict[str, Any] = {"ok": response.status_code < 400,
                              "status": response.status_code,
                              "truncated": truncated,
                              "body": text[:MAX_RESPONSE_CHARS]}
    if not answer["ok"]:
        answer["error"] = (f"{operation['name']} returned {response.status_code}: "
                           f"{text[:300] or 'no detail'}")
    return answer
