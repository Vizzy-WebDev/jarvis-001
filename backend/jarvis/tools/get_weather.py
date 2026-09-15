"""Current weather for a place, via Open-Meteo — free, no account, no key."""

from __future__ import annotations

from urllib.parse import quote

from ..capabilities import CapabilitySpec, Risk
from ._http import get_json

WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "light snow showers", 86: "heavy snow showers", 95: "a thunderstorm",
    96: "a thunderstorm with some hail", 99: "a severe thunderstorm with hail",
}


def _geocode(place: str) -> dict | None:
    data = get_json(f"https://geocoding-api.open-meteo.com/v1/search?name={quote(place)}&count=1")
    hit = (data.get("results") or [None])[0]
    if not hit:
        return None
    label = ", ".join(p for p in (hit.get("name"), hit.get("admin1"), hit.get("country")) if p)
    return {"lat": hit["latitude"], "lon": hit["longitude"], "label": label}


def _run(place: str = "") -> dict:
    query = str(place or "").strip()
    if not query:
        return {"ok": False, "error": "No place given."}
    try:
        location = _geocode(query)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "Couldn't reach the weather service."}
    if not location:
        return {"ok": False, "error": f'I couldn\'t find a place called "{query}".'}

    try:
        data = get_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={location['lat']}&longitude={location['lon']}"
            "&current=temperature_2m,weather_code&temperature_unit=fahrenheit")
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The weather service is unavailable right now."}

    current = data.get("current") or {}
    if current.get("temperature_2m") is None:
        return {"ok": False, "error": "No weather data available for that place."}
    return {
        "ok": True,
        "place": location["label"],
        "temperatureF": round(current["temperature_2m"]),
        "condition": WEATHER_CODES.get(current.get("weather_code"), "unknown conditions"),
    }


SPEC = CapabilitySpec(
    id="builtin.get_weather",
    name="get_weather",
    description=("Get the current weather for a place. Use this when the user asks about the "
                 "weather, temperature, or whether it's raining or snowing somewhere."),
    input_schema={"type": "object", "properties": {
        "place": {"type": "string",
                  "description": 'City or place name, e.g. "Lagos" or "Boston, MA". '
                                 "If unclear, ask the user which place they mean."}},
        "required": ["place"]},
    risk=Risk.LOW,
    handler=_run,
    timeout_s=20.0,
    tags=frozenset({"core"}),
)
