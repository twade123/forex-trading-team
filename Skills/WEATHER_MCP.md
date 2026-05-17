# Weather MCP Skill — handler_weather (OpenWeather API)

Complete reference for the Weather MCP. Covers everything this tool can do, for any use case.

---

## Connection
- **Geocoding API**: `http://api.openweathermap.org/geo/1.0/direct`
- **OneCall 3.0 API**: `https://api.openweathermap.org/data/3.0/onecall`
- **Key**: `~/jarvis/API/OPENWEATHER_API_KEY.txt`
- **Free tier**: 60 calls/min, 1,000 calls/day
- **Excludes**: minutely data (excluded by default in handler)

---

## Functions

### weather(location, units, lang) — Entry Point

Main function. Resolves location name → lat/lon → fetches full OneCall 3.0 data.

```python
# handler_weather.weather(location, units, lang)
# Async — handles geocoding internally

params: {
    location: str,     # REQUIRED. "City" or "City,CountryCode" (e.g. "Sydney,AU", "London,GB", "Calgary,CA")
    units: str,        # "metric" (°C, m/s) | "imperial" (°F, mph) | "standard" (K, m/s). Default: "metric"
    lang: str          # Language code: "en", "es", "fr", "de", "zh_cn", etc. Default: "en"
}
```

### Response Format (OneCall 3.0)
```json
{
    "lat": -33.8698,
    "lon": 151.2083,
    "timezone": "Australia/Sydney",
    "timezone_offset": 39600,

    "current": {
        "dt": 1739750400,
        "sunrise": 1739735000,
        "sunset": 1739782000,
        "temp": 26.12,
        "feels_like": 26.12,
        "pressure": 1015,
        "humidity": 54,
        "dew_point": 16.2,
        "uvi": 8.5,
        "clouds": 20,
        "visibility": 10000,
        "wind_speed": 5.14,
        "wind_deg": 180,
        "wind_gust": 8.23,
        "weather": [
            {
                "id": 802,
                "main": "Clouds",
                "description": "scattered clouds",
                "icon": "03d"
            }
        ]
    },

    "hourly": [
        {
            "dt": 1739750400,
            "temp": 25.5,
            "feels_like": 25.8,
            "pressure": 1015,
            "humidity": 60,
            "dew_point": 17.1,
            "uvi": 6.2,
            "clouds": 40,
            "visibility": 10000,
            "wind_speed": 4.8,
            "wind_deg": 190,
            "wind_gust": 7.5,
            "pop": 0.2,
            "weather": [{"id": 500, "main": "Rain", "description": "light rain", "icon": "10d"}],
            "rain": {"1h": 0.5}
        }
    ],

    "daily": [
        {
            "dt": 1739793600,
            "sunrise": 1739735000,
            "sunset": 1739782000,
            "moonrise": 1739760000,
            "moonset": 1739800000,
            "moon_phase": 0.75,
            "summary": "Expect a day of partly cloudy with rain",
            "temp": {
                "day": 26.12,
                "min": 16.66,
                "max": 28.42,
                "night": 18.5,
                "eve": 22.3,
                "morn": 17.1
            },
            "feels_like": {"day": 26.12, "night": 18.0, "eve": 22.0, "morn": 16.8},
            "pressure": 1014,
            "humidity": 50,
            "dew_point": 15.0,
            "wind_speed": 6.2,
            "wind_deg": 200,
            "wind_gust": 10.1,
            "clouds": 35,
            "pop": 0.0,
            "uvi": 9.0,
            "weather": [{"id": 802, "main": "Clouds", "description": "scattered clouds", "icon": "03d"}]
        }
    ],

    "alerts": [
        {
            "sender_name": "Bureau of Meteorology",
            "event": "Severe Thunderstorm Warning",
            "start": 1739750400,
            "end": 1739793600,
            "description": "Severe thunderstorms expected with damaging winds and heavy rainfall...",
            "tags": ["Thunderstorm", "Wind"]
        }
    ]
}
```

**Key fields:**
- `current` — right now: temp, humidity, wind, clouds, UV index, visibility
- `hourly` — 48-hour forecast, hourly resolution. `pop` = probability of precipitation (0.0-1.0)
- `daily` — 8-day forecast. Includes min/max temp, summary text, moon phase
- `alerts` — **only present when active alerts exist**. Array can be empty or missing entirely.
- `rain` / `snow` — only present in hourly/current when precipitation is occurring. Contains `{"1h": mm}`

### Weather Categories (`weather[].main`)
| main | Description |
|------|-------------|
| Clear | Clear sky |
| Clouds | Cloudy (few, scattered, broken, overcast) |
| Rain | Rain (light, moderate, heavy, extreme) |
| Drizzle | Light drizzle |
| Thunderstorm | Thunderstorms (with or without rain) |
| Snow | Snow (light, moderate, heavy) |
| Mist | Mist (visibility reduced) |
| Fog | Fog (visibility < 1km) |
| Haze | Haze |
| Dust | Dust/sandstorm |
| Tornado | Tornado |

---

## Geocoding

The handler resolves location names automatically via OpenWeather's geocoding API. Format options:

| Format | Example | Notes |
|--------|---------|-------|
| City | `"Sydney"` | Returns first match globally |
| City,Country | `"Sydney,AU"` | Specific country — recommended |
| City,State,Country | `"Portland,OR,US"` | Disambiguate US cities |

Returns `(lat, lon)` tuple. Returns `None` if location can't be resolved.

**Tip:** Use country codes to avoid ambiguity. "London" could be UK or Ontario.

---

## Units

| Unit System | Temp | Wind | Pressure |
|-------------|------|------|----------|
| `"metric"` | °C | m/s | hPa |
| `"imperial"` | °F | mph | hPa |
| `"standard"` | K | m/s | hPa |

Wind speed reference (metric):
- < 5 m/s: light breeze
- 5-10 m/s: moderate wind
- 10-20 m/s: strong wind
- 20-30 m/s: gale/storm force
- 30+ m/s: hurricane force

---

## Rate Limits & Best Practices
- **60 calls/min, 1,000/day** on free tier
- **Cache results for 15-30 minutes** — weather doesn't change that fast
- **Batch locations** — if checking multiple cities, space requests to avoid rate limits
- **Country codes recommended** — avoids geocoding ambiguity
- **Alerts array may be absent** — check for existence before accessing
- **`pop` in hourly** — probability of precipitation, 0.0 to 1.0 (0% to 100%)

---

## Confirmed Live Behavior (Feb 2026)
- `weather(location="Sydney,AU")` → temp=26.12°C, humidity=54%, wind=5.14 m/s, Clouds ✅
- Geocoding resolves "Sydney,AU" → correct lat/lon ✅
- 8 daily forecasts returned ✅
- 48 hourly forecasts returned ✅
- Alerts array empty when no active alerts ✅
- Returns JSON string (handler calls `json.dumps`) ✅

---

## Error Handling
- **Location not found**: Returns `"Error: Could not resolve location 'xyz'."`
- **No location provided**: Returns `"Error: Location is required for weather queries."`
- **API failure**: Returns `"Error: Failed to fetch weather data."`
- **HTTP 401**: Invalid API key
- **HTTP 429**: Rate limit exceeded
