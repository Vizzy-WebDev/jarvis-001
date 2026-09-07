"""The whole plug-in surface for a diagnostic check. Pure, zero-import.

Mirrors the heartbeat's source registry deliberately: the thing that runs checks
knows nothing about what any check does, so a new failure mode to watch for is
one `register_check()` call and never a change to the dispatcher.

* `probe()` returns `{"ok": bool, "detail"?: str}`. `detail` is plain language,
  because it is surfaced to a person more or less verbatim.
* `remedy()` is optional. A check with none escalates straight from a failed
  probe, with no attempt in between — which is correct for anything detection
  only, where a fix is not this build's call to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Check:
    id: str
    probe: Callable[[], dict[str, Any]]
    remedy: Callable[[], Any] | None = None
    #: 'malfunction' or 'security'. Security checks are detection only, by
    #: explicit scope: a build that silently "fixes" a security anomaly is a
    #: build that can destroy the evidence of one.
    kind: str = "malfunction"


_checks: dict[str, Check] = {}


def register_check(check: Check) -> None:
    if not getattr(check, "id", None):
        raise ValueError("a diagnostic check needs an id")
    if not callable(check.probe):
        raise TypeError(f'check "{check.id}" must implement probe()')
    if check.kind == "security" and check.remedy is not None:
        raise ValueError(f'check "{check.id}": security checks are detection only')
    _checks[check.id] = check


def list_checks() -> list[Check]:
    return list(_checks.values())


def get_check(check_id: str) -> Check | None:
    return _checks.get(check_id)


def reset() -> None:
    _checks.clear()
