// Skill: current weather for a place, via Open-Meteo — a free public API,
// no account or key required. First geocodes the place name to
// coordinates, then asks for current conditions. Also used directly (not
// through the model) by the morning briefing composer, since it's a plain
// fact-gathering call rather than something that needs the AI in the loop.

const WEATHER_CODES = {
  0: 'clear sky',
  1: 'mostly clear',
  2: 'partly cloudy',
  3: 'overcast',
  45: 'foggy',
  48: 'foggy',
  51: 'light drizzle',
  53: 'drizzle',
  55: 'heavy drizzle',
  61: 'light rain',
  63: 'rain',
  65: 'heavy rain',
  66: 'freezing rain',
  67: 'heavy freezing rain',
  71: 'light snow',
  73: 'snow',
  75: 'heavy snow',
  77: 'snow grains',
  80: 'light showers',
  81: 'showers',
  82: 'heavy showers',
  85: 'light snow showers',
  86: 'heavy snow showers',
  95: 'a thunderstorm',
  96: 'a thunderstorm with some hail',
  99: 'a severe thunderstorm with hail',
};

async function geocode(place) {
  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(place)}&count=1`;
  const res = await fetch(url);
  if (!res.ok) return null;
  const data = await res.json();
  const hit = data?.results?.[0];
  if (!hit) return null;
  return { lat: hit.latitude, lon: hit.longitude, label: [hit.name, hit.admin1, hit.country].filter(Boolean).join(', ') };
}

export default {
  name: 'get_weather',
  description:
    "Get the current weather for a place. Use this when the user asks about the weather, " +
    "temperature, or whether it's raining/snowing somewhere.",
  parameters: {
    type: 'object',
    properties: {
      place: {
        type: 'string',
        description: 'City or place name, e.g. "Lagos" or "Boston, MA". If unclear, ask the user which place they mean.',
      },
    },
    required: ['place'],
  },
  async run({ place } = {}) {
    const query = String(place || '').trim();
    if (!query) return { ok: false, error: 'No place given.' };

    let loc;
    try {
      loc = await geocode(query);
    } catch {
      return { ok: false, error: "Couldn't reach the weather service." };
    }
    if (!loc) return { ok: false, error: `I couldn't find a place called "${query}".` };

    try {
      const url = `https://api.open-meteo.com/v1/forecast?latitude=${loc.lat}&longitude=${loc.lon}&current=temperature_2m,weather_code&temperature_unit=fahrenheit`;
      const res = await fetch(url);
      if (!res.ok) return { ok: false, error: 'The weather service is unavailable right now.' };
      const data = await res.json();
      const tempF = data?.current?.temperature_2m;
      const code = data?.current?.weather_code;
      if (tempF === undefined) return { ok: false, error: 'No weather data available for that place.' };
      return {
        ok: true,
        place: loc.label,
        temperatureF: Math.round(tempF),
        condition: WEATHER_CODES[code] || 'unknown conditions',
      };
    } catch {
      return { ok: false, error: "Couldn't reach the weather service." };
    }
  },
};
