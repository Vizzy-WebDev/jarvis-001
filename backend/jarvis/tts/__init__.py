"""Server-side speech, provider-agnostic.

One registry, one contract, so a new paid provider is one new adapter plus a
key — never a change to anything that calls this. Every adapter exports the same
four things: `is_configured()`, `stream(text, voice=, ref=)` as a GENERATOR,
`matches_ref(ref)`, and `test_key(key, ref)`.

`stream()` is a generator on every provider, including ones whose API is a
single request that yields exactly one chunk. That is deliberate: a genuinely
streaming provider added later needs no change in any caller, because the
consumer already iterates either way.

**Providers are matched to CONFIGURED SERVICES by asking, never by a name this
file knows.** A service's ref is whatever the user typed, slugified, so this
module hardcodes no provider name anywhere — it asks each registered adapter "is
this one yours?" and defers entirely to the answer.

**The browser's own voice is deliberately not a provider here.** It has no
server component at all — the browser talks to nothing — so it lives entirely
client-side, and it is the one voice that is always available.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from . import elevenlabs, generic
from .elevenlabs import NoKey

#: A real dedicated adapter always wins the match for the company it was built
#: for, so the data-driven registry is asked last. It only recognises names it
#: actually knows, so there is no real collision either way.
ADAPTERS = [elevenlabs, generic]


def list_providers() -> list[dict[str, Any]]:
    """Every configured service a real adapter recognises, for the voice picker.

    A flat expansion rather than one match per adapter: `generic` can legitimately
    match SEVERAL configured services at once, each its own endpoint, so "the
    first service this adapter matches" would quietly drop the rest.
    """
    services = external_services_list()
    return [{"id": service["ref"], "label": service["label"],
             "configured": service["configured"]}
            for adapter in ADAPTERS
            for service in services if adapter.matches_ref(service["ref"])]


def external_services_list() -> list[dict[str, Any]]:
    from .. import external_services

    return external_services.list_services()


def _resolve(provider_id: str | None) -> tuple[Any, str | None]:
    """Both the adapter AND the exact ref that matched.

    Knowing which adapter is not enough on its own: the data-driven one can match
    many configured services, each with its own endpoint, so the ref says which
    connection this call actually means.
    """
    from ..prefs import get_prefs

    ref = provider_id or get_prefs().get("ttsProvider")
    if not ref:
        return None, None
    return next((a for a in ADAPTERS if a.matches_ref(ref)), None), ref


def is_configured(provider_id: str | None = None) -> bool:
    adapter, _ = _resolve(provider_id)
    return bool(adapter and adapter.is_configured())


def stream(text: str, *, provider: str | None = None,
           voice: str | None = None) -> Iterator[dict[str, Any]]:
    adapter, ref = _resolve(provider)
    if adapter is None:
        raise NoKey("No TTS provider is configured.")

    # Recorded once resolution succeeds and synthesis is genuinely about to be
    # attempted: most providers bill per character REQUESTED, not per chunk our
    # own playback happens to consume.
    from ..cost import store as cost_store

    try:
        cost_store.record_event(provider=ref or "", unit_kind="characters",
                                units_out=len(str(text or "")))
    except Exception:  # noqa: BLE001 — bookkeeping must never stop speech
        pass
    yield from adapter.stream(text, voice=voice, ref=ref)


def tester_for(ref: str) -> Callable[[str], dict[str, Any]] | None:
    """The live key check for whichever adapter recognises this ref, or None —
    the generic external-service test route turns None into an honest "no live
    test available" rather than a pass."""
    adapter = next((a for a in ADAPTERS if a.matches_ref(ref)), None)
    return (lambda key: adapter.test_key(key, ref)) if adapter else None
