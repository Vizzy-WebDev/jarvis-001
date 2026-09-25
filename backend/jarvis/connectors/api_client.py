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
from typing import Any, Callable
from urllib.parse import urljoin

from ..redact import redact_text

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
BODY_METHODS = ("post", "put", "patch")
MAX_RESPONSE_CHARS = 20000
TIMEOUT_S = 30.0

_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


def sanitise_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", str(name or "")).strip("_").lower()
    return (cleaned or "operation")[:60]


#: A request body's schema is shown to the model in full when it is this small or
#: smaller once serialised; a larger one collapses to a plain object described by
#: the operation's own text, rather than flooding every turn that declares it.
MAX_BODY_SCHEMA_CHARS = 4000
_SCHEMA_KEYS = ("type", "description", "enum", "items", "properties", "required",
                "default", "format", "minimum", "maximum")


def _parse_document(response: Any) -> Any:
    """JSON first, then YAML — many services publish only a YAML spec."""
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        pass
    text = getattr(response, "text", "") or ""
    try:
        import yaml

        parsed = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        parsed = None
    if not isinstance(parsed, dict):
        raise ValueError(
            "That doesn't look like an OpenAPI document (JSON or YAML). Check the address, "
            "or add endpoints by hand.")
    return parsed


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    """A schema with its local `$ref`s filled in, trimmed to what a model needs."""
    if depth > 6 or not isinstance(node, dict):
        return {"type": "object"} if depth > 6 else node
    if "$ref" in node:
        ref = str(node["$ref"])
        target: Any = spec
        if ref.startswith("#/"):
            for part in ref[2:].split("/"):
                target = target.get(part) if isinstance(target, dict) else None
        return _resolve(spec, target or {"type": "object"}, depth + 1)
    for combiner in ("allOf", "oneOf", "anyOf"):
        if isinstance(node.get(combiner), list) and node[combiner]:
            merged: dict[str, Any] = {"type": "object", "properties": {}}
            for part in node[combiner] if combiner == "allOf" else node[combiner][:1]:
                resolved = _resolve(spec, part, depth + 1)
                if isinstance(resolved, dict):
                    merged["properties"].update(resolved.get("properties") or {})
                    merged["required"] = sorted(set(merged.get("required", []))
                                                | set(resolved.get("required") or []))
                    if resolved.get("type") and resolved["type"] != "object":
                        return resolved
            return merged
    out: dict[str, Any] = {}
    for key in _SCHEMA_KEYS:
        if key not in node:
            continue
        value = node[key]
        if key == "properties" and isinstance(value, dict):
            value = {k: _resolve(spec, v, depth + 1) for k, v in value.items()}
        elif key == "items":
            value = _resolve(spec, value, depth + 1)
        elif key == "description":
            value = str(value)[:300]
        out[key] = value
    if out.get("type") == "array" and not isinstance(out.get("items"), dict):
        # A JSON Schema array with no `items` is legal and some providers refuse
        # the whole request over it; say what it holds.
        out["items"] = {"type": "string"}
    if "type" not in out and "properties" in out:
        out["type"] = "object"
    return out


def _parameter_schema(spec: dict[str, Any], parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {
        k: parameter[k] for k in ("type", "enum", "items", "format") if k in parameter}
    described = _resolve(spec, schema or {"type": "string"})
    if described.get("type") not in ("string", "number", "integer", "boolean", "array"):
        described = {"type": "string"}
    if parameter.get("description"):
        described["description"] = str(parameter["description"])[:300]
    return described


def _body_schema(spec: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any] | None:
    body = operation.get("requestBody")
    if isinstance(body, dict) and "$ref" in body:
        body = _resolve(spec, body)
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
        return {"type": "object", "description": "The request body, as JSON."}
    import json as _json

    schema = _resolve(spec, media["schema"])
    if schema.get("type") != "object" or len(_json.dumps(schema)) > MAX_BODY_SCHEMA_CHARS:
        return {"type": "object", "description": "The request body, as JSON."}
    schema.setdefault("description", "The request body, as JSON.")
    return schema


def _auth_from(spec: dict[str, Any]) -> dict[str, Any] | None:
    """How the spec itself says a key is sent — bearer, header or query."""
    schemes = ((spec.get("components") or {}).get("securitySchemes")
               or spec.get("securityDefinitions") or {})
    for scheme in schemes.values() if isinstance(schemes, dict) else []:
        if not isinstance(scheme, dict):
            continue
        kind = str(scheme.get("type") or "").lower()
        if kind == "http" and str(scheme.get("scheme") or "").lower() == "bearer":
            return {"kind": "bearer"}
        if kind == "apikey" and scheme.get("name"):
            where = str(scheme.get("in") or "header").lower()
            if where == "header" and str(scheme["name"]).lower() == "authorization":
                return {"kind": "bearer"}
            return {"kind": "query" if where == "query" else "header",
                    "name": str(scheme["name"])}
    return None


def discover_from_spec(spec_url: str, *, fetch: Any = None) -> dict[str, Any]:
    """Read an OpenAPI/Swagger document (JSON or YAML) into proposed operations.

    Both live shapes are handled, because both are still common: OpenAPI 3's
    `servers[0].url` — often relative, so resolved against the document's own
    address — and Swagger 2's `host`/`basePath`/`schemes`. Normalised to one
    absolute base URL here, so calling an operation never has to care which.
    Parameters keep their real types and a request body keeps its real fields,
    so the model is told what to send rather than handed an empty object.
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

    spec = _parse_document(response)
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
        shared = [p for p in methods.get("parameters") or [] if isinstance(p, dict)]
        for method in HTTP_METHODS:
            operation = methods.get(method)
            if not isinstance(operation, dict):
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            for parameter in shared + list(operation.get("parameters") or []):
                parameter = _resolve(spec, parameter) if isinstance(parameter, dict) \
                    and "$ref" in parameter else parameter
                if not isinstance(parameter, dict) or not parameter.get("name"):
                    continue
                if parameter.get("in") in ("header", "cookie"):
                    continue  # the key is sent by the connector, never by the model
                if parameter.get("in") == "body" and isinstance(parameter.get("schema"), dict):
                    properties["body"] = _resolve(spec, parameter["schema"])
                    continue
                properties[parameter["name"]] = _parameter_schema(spec, parameter)
                if parameter.get("required"):
                    required.append(parameter["name"])
            if method in BODY_METHODS:
                body = _body_schema(spec, operation)
                if body is not None:
                    properties["body"] = body
                    if (operation.get("requestBody") or {}).get("required"):
                        required.append("body")
            operations.append({
                "name": sanitise_name(operation.get("operationId")
                                      or f"{method}_{path_template}"),
                "description": str(operation.get("summary") or operation.get("description")
                                   or f"{method.upper()} {path_template}")[:500],
                "method": method.upper(),
                "path": path_template,
                "parameters": {"type": "object", "properties": properties,
                               "required": required},
            })

    if not operations:
        raise ValueError("No usable endpoints were found in that document.")
    return {"baseUrl": base_url, "auth": _auth_from(spec), "operations": operations}


def tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": op["name"], "description": op.get("description") or op["name"],
             "parameters": op.get("parameters") or {"type": "object", "properties": {}}}
            for op in (config or {}).get("operations") or []]


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


def _field(data: Any, dotted: str | None) -> Any:
    """`data.taskId`-style lookup into a JSON reply; None when any step is missing."""
    if not dotted:
        return None
    current = data
    for part in str(dotted).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _fill_path(path: str, remaining: dict[str, Any]) -> str:
    # Path placeholders are filled from the arguments and removed, so what is
    # left is a query string rather than being sent twice.
    for key in list(remaining):
        placeholder = "{" + key + "}"
        if placeholder in path:
            path = path.replace(placeholder, str(remaining.pop(key)))
    return path


def _default_request(**kw: Any) -> Any:  # noqa: ANN401
    import httpx

    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
        return client.request(**kw)


def _send(config: dict[str, Any], method: str, path: str, params: dict[str, Any],
          body: Any, request: Any) -> Any:
    base_url = (config or {}).get("baseUrl")
    if not base_url:
        raise ValueError("That connector has no address to call.")
    headers: dict[str, str] = {"Accept": "application/json"}
    query: dict[str, Any] = {}
    _authorise(config, headers, query)
    query.update(params)
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    return request(method=method, url=url, headers=headers, params=query,
                   json=body if body is not None else None)


def _outcome(config: dict[str, Any], response: Any, label: str) -> dict[str, Any]:
    """What a reply means, in one shape: `{ok, status, truncated, body[, error]}`.

    Two failure signals, both real: an HTTP error status, and — for services that
    always answer 200 and put the verdict in the body — the connector's
    `responseCheck` (`{"field": "code", "ok": [200], "message": "msg"}`), which is
    DATA about that service, not code that knows its name.
    """
    # Whatever comes back goes to the model, and the key has just been sent — in
    # a header, or (for `kind == "query"`) in the URL itself, which is what an
    # error body quotes back most often. Redacted before it can be read, logged
    # or saved into the conversation.
    text = str(redact_text(getattr(response, "text", "") or "") or "")
    truncated = len(text) > MAX_RESPONSE_CHARS
    answer: dict[str, Any] = {"ok": response.status_code < 400,
                              "status": response.status_code,
                              "truncated": truncated,
                              "body": text[:MAX_RESPONSE_CHARS]}
    if not answer["ok"]:
        answer["error"] = f"{label} returned {response.status_code}: {text[:300] or 'no detail'}"
        return answer
    check = (config or {}).get("responseCheck") or {}
    if check.get("field"):
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 — not JSON: nothing to check
            return answer
        value = _field(data, check["field"])
        allowed = check.get("ok") or []
        if value is not None and allowed and value not in allowed:
            message = _field(data, check.get("message")) or f"{check['field']} = {value}"
            answer["ok"] = False
            answer["error"] = f"{label} failed: {str(redact_text(str(message)))[:300]}"
    return answer


def _await_result(config: dict[str, Any], operation: dict[str, Any], first: Any,
                  request: Any, sleep: Callable[[float], None],
                  clock: Callable[[], float]) -> dict[str, Any]:
    """Follow a job the service runs in the background until it is finished.

    Described entirely by the operation's `wait` block — where the job id is in
    the first reply, which endpoint reports progress, where its status field is,
    and which values mean finished or failed — so any service that works this way
    fits, and nothing here knows which one it is. Bounded: past `timeoutS` the
    job id is handed back so it can be checked again later, never waited on
    forever and never reported as done when it is not.
    """
    wait = operation.get("wait") or {}
    label = operation["name"]
    try:
        first_data = first.json()
    except Exception:  # noqa: BLE001
        first_data = None
    job_id = _field(first_data, wait.get("idFrom"))
    if job_id in (None, ""):
        return {"ok": False, "status": first.status_code,
                "error": f"{label} started, but its reply had no job id at "
                         f"\"{wait.get('idFrom')}\" to follow."}

    interval = max(1.0, float(wait.get("intervalS") or 5))
    deadline = clock() + max(10.0, float(wait.get("timeoutS") or 600))
    done = [str(v).lower() for v in wait.get("done") or []]
    failed = [str(v).lower() for v in wait.get("failed") or []]
    method = str(wait.get("method") or "GET").upper()
    last_status: Any = None
    while True:
        values = {"id": str(job_id)}
        path = str(wait.get("path") or "")
        for key, value in values.items():
            path = path.replace("{" + key + "}", value)
        params = {k: str(v).replace("{id}", str(job_id))
                  for k, v in (wait.get("query") or {}).items()}
        try:
            response = _send(config, method, path, params, None, request)
            outcome = _outcome(config, response, label)
            data = response.json() if outcome["ok"] else None
        except Exception:  # noqa: BLE001 — one failed check is not the job failing
            outcome, data = None, None
        if data is not None:
            last_status = _field(data, wait.get("statusField"))
            state = str(last_status).lower() if last_status is not None else ""
            if state in done:
                return {**outcome, "jobId": str(job_id), "jobStatus": last_status}
            if state in failed:
                return {**outcome, "ok": False, "jobId": str(job_id), "jobStatus": last_status,
                        "error": f"{label} finished as \"{last_status}\", not successfully."}
        if clock() >= deadline:
            return {"ok": True, "pending": True, "jobId": str(job_id), "jobStatus": last_status,
                    "note": (f"Still running after {int(float(wait.get('timeoutS') or 600))}s "
                             f"(status: {last_status or 'unknown'}). It has not finished yet; "
                             f"job id {job_id} can be checked again later.")}
        sleep(interval)


def dispatch(name: str, args: dict[str, Any], config: dict[str, Any], *,
             request: Any = None, sleep: Callable[[float], None] | None = None,
             clock: Callable[[], float] | None = None) -> dict[str, Any]:
    operation = next((o for o in (config or {}).get("operations") or []
                      if o.get("name") == name), None)
    if operation is None:
        raise KeyError(f"Unknown operation: {name}")

    remaining = dict(args or {})
    body = remaining.pop("body", None)
    path = _fill_path(operation["path"], remaining)
    request = request or _default_request

    response = _send(config, operation["method"], path, remaining, body, request)
    answer = _outcome(config, response, operation["name"])
    if not answer["ok"] or not operation.get("wait"):
        return answer

    import time

    return _await_result(config, operation, response, request,
                         sleep or time.sleep, clock or time.monotonic)


def check(config: dict[str, Any], *, request: Any = None) -> dict[str, Any]:
    """Whether this API connection works right now: `{ok, detail}`.

    Runs the connector's own `test` (`{"method", "path", "okStatus"}`) — one cheap
    call the service answers differently for a good key and a bad one. With no
    test set, nothing can be proven, and it says so rather than claiming success.
    """
    test = (config or {}).get("test") or {}
    if not test.get("path"):
        return {"ok": True, "untested": True,
                "detail": "No connection test is set for this connector, so it wasn't checked."}
    request = request or _default_request
    try:
        response = _send(config, str(test.get("method") or "GET").upper(),
                         str(test["path"]), dict(test.get("query") or {}), None, request)
    except ValueError:
        raise
    except Exception as err:  # noqa: BLE001 — unreachable is a result
        return {"ok": False, "detail": f"Couldn't reach {config.get('baseUrl')}: "
                                       f"{str(redact_text(str(err)))[:200]}"}
    status = response.status_code
    allowed = [int(s) for s in test.get("okStatus") or []]
    if status in (401, 403):
        return {"ok": False, "detail": f"The service refused the key ({status}). "
                                       "Check it and save it again."}
    if status == 402:
        return {"ok": False, "detail": "The service says the account has no credits left (402)."}
    if (allowed and status not in allowed) or (not allowed and status >= 400):
        text = str(redact_text(getattr(response, "text", "") or ""))[:200]
        return {"ok": False, "detail": f"The test call answered {status}: {text or 'no detail'}"}
    if status < 400:
        outcome = _outcome(config, response, "The test call")
        if not outcome["ok"]:
            return {"ok": False, "detail": outcome["error"]}
    return {"ok": True, "detail": None}


# --- which operations an API offers ------------------------------------------------

def validate_operations(operations: Any) -> list[dict[str, Any]]:
    """Operations, checked and normalised; raises ValueError.

    Path placeholders become required parameters when not described, so a hand-
    added `/pet/{petId}` works without anyone writing its schema. `wait`, when
    present, must say where the job id is and which endpoint reports progress.
    """
    if not isinstance(operations, list):
        raise ValueError("Operations must be a list.")
    out, seen = [], set()
    for raw in operations:
        if not isinstance(raw, dict):
            raise ValueError("Each operation must be an object.")
        name = sanitise_name(raw.get("name") or "")
        if not raw.get("name") or name in seen:
            raise ValueError(f'Every operation needs its own name ("{raw.get("name")}").')
        seen.add(name)
        method = str(raw.get("method") or "GET").upper()
        if method.lower() not in HTTP_METHODS:
            raise ValueError(f'"{name}": {method} is not an HTTP method this can send.')
        path = str(raw.get("path") or "").strip()
        if not path.startswith("/"):
            raise ValueError(f'"{name}": the path must start with "/".')
        parameters = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
        properties = dict(parameters.get("properties") or {})
        required = [r for r in parameters.get("required") or [] if isinstance(r, str)]
        for key in re.findall(r"\{([^}]+)\}", path):
            properties.setdefault(key, {"type": "string", "description": f"The {key}."})
            if key not in required:
                required.append(key)
        operation = {"name": name,
                     "description": str(raw.get("description") or f"{method} {path}").strip(),
                     "method": method, "path": path,
                     "parameters": {"type": "object", "properties": properties,
                                    "required": required}}
        wait = raw.get("wait")
        if wait:
            if not isinstance(wait, dict) or not wait.get("idFrom") or not wait.get("path") \
                    or not wait.get("statusField") or not wait.get("done"):
                raise ValueError(f'"{name}": waiting for a result needs idFrom, path, '
                                 "statusField and done.")
            operation["wait"] = wait
        out.append(operation)
    return out
