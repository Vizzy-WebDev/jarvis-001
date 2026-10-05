"""A trace record for every call, and spend queries over them.

Each record (table `model_traces`, migration 36) holds: the request id, task
class, affinity key and data class; which endpoints were eligible, in rank order,
and why every other one was rejected; the attempts and the fallbacks; the chosen
endpoint and the model the server reported; time to first token and in total;
usage and cost; the feature report; and outcome signals (schema repair used or
failed, invalid tool arguments, fallbacks). Metadata only, unless config's
`settings.trace_content` is on — then the request's and response's content too.

Recording never fails a call: a write that can't happen is logged and dropped.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from . import config
from .types import ImagePart

logger = logging.getLogger(__name__)
_lock = threading.RLock()

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS model_traces (
  request_id       TEXT PRIMARY KEY,
  at               TEXT NOT NULL,
  task_class       TEXT NOT NULL,
  data_class       TEXT NOT NULL,
  affinity_key     TEXT,
  outcome          TEXT NOT NULL,
  endpoint_id      TEXT,
  connection       TEXT,
  reported_model   TEXT,
  ttft_ms          INTEGER,
  total_ms         INTEGER,
  input_tokens     INTEGER,
  output_tokens    INTEGER,
  cached_tokens    INTEGER,
  reasoning_tokens INTEGER,
  cost             REAL,
  detail_json      TEXT NOT NULL,
  content_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_traces_at ON model_traces(at);
CREATE INDEX IF NOT EXISTS idx_model_traces_connection ON model_traces(connection);
CREATE INDEX IF NOT EXISTS idx_model_traces_task_class ON model_traces(task_class);
"""


def _plain(value: Any) -> Any:
    if isinstance(value, ImagePart):
        return {"image": value.mime, "bytes_b64": len(value.data_b64)}  # the picture itself is never kept here
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(getattr(value, k)) for k in value.__dataclass_fields__ if k != "mint_id"}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write(record: Any) -> None:
    """execute.py's observer: one row per finished call."""
    from ..db import get_db

    try:
        content_on = config.current().settings.trace_content
    except config.ConfigError:
        content_on = False
    usage = record.usage
    detail = {
        "ranked": record.ranked,
        "rejected": [{"endpoint": r.endpoint_id, "reason": r.reason, "detail": r.detail} for r in record.rejected],
        "attempts": [asdict(a) for a in record.attempts],
        "fallbacks": [asdict(f) for f in record.fallbacks],
        "report": {"features": dict(record.report.features), "warnings": list(record.report.warnings),
                   "mapped": {k: dict(v) for k, v in record.report.mapped.items()}}
        if record.report else None,
        "signals": record.signals,
    }
    content = None
    if content_on:
        content = json.dumps({"request": _plain(record.request), "response": _plain(record.response)},
                             ensure_ascii=False, default=str)
    row = (record.request_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), record.task_class,
           record.data_class, record.affinity_key, record.outcome, record.endpoint_id,
           record.endpoint_id.split("/", 1)[0] if record.endpoint_id else None, record.reported_model,
           record.ttft_ms, record.total_ms,
           usage.input if usage else None, usage.output if usage else None, usage.cached if usage else None,
           usage.reasoning if usage else None, usage.cost if usage else None,
           json.dumps(detail, ensure_ascii=False, default=str), content)
    try:
        with _lock:
            get_db().execute(
                "INSERT OR REPLACE INTO model_traces (request_id, at, task_class, data_class, affinity_key, outcome, "
                "endpoint_id, connection, reported_model, ttft_ms, total_ms, input_tokens, output_tokens, "
                "cached_tokens, reasoning_tokens, cost, detail_json, content_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    except Exception:  # noqa: BLE001 - a trace must never cost anyone their answer
        logger.exception("couldn't write a model trace")


def recent(limit: int = 50) -> list[dict[str, Any]]:
    from ..db import get_db

    with _lock:
        rows = get_db().execute("SELECT * FROM model_traces ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        entry["detail"] = json.loads(entry.pop("detail_json") or "{}")
        content = entry.pop("content_json")
        entry["content"] = json.loads(content) if content else None
        out.append(entry)
    return out


_GROUPS = {"connection": "connection", "task_class": "task_class", "day": "substr(at, 1, 10)",
           "endpoint": "endpoint_id"}


def spend(by: str = "day", *, since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
    """What calls cost, grouped by connection, task class, day or endpoint.
    `unpriced_calls` counts answered calls whose price isn't known — never folded in
    as zero."""
    if by not in _GROUPS:
        raise ValueError(f"spend can be grouped by {', '.join(_GROUPS)}")
    from ..db import get_db

    where, params = ["outcome = 'ok'"], []
    if since:
        where.append("at >= ?")
        params.append(since)
    if until:
        where.append("at < ?")
        params.append(until)
    sql = (f"SELECT {_GROUPS[by]} AS key, COUNT(*) AS calls, COALESCE(SUM(cost), 0) AS cost, "
           "SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls, "
           "COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens "
           f"FROM model_traces WHERE {' AND '.join(where)} GROUP BY key ORDER BY key")
    with _lock:
        return [dict(r) for r in get_db().execute(sql, params).fetchall()]
