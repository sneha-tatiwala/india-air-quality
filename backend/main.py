import json
import os
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

from . import cache, openaq

app = FastAPI(title="India Air Quality API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Load stations from saved JSON on startup (no API call needed)
# ---------------------------------------------------------------------------
LOCATIONS_FILE = Path(__file__).parent.parent / "india_locations.json"

def _parse_city(name: str) -> str:
    """Extract city from CPCB-style station names.
    'Anand Vihar, New Delhi - DPCC'  →  'New Delhi'
    'Peenya, Bengaluru - KSPCB'      →  'Bengaluru'
    'IGI Airport'                    →  'IGI Airport'
    """
    if " - " in name:
        name = name.rsplit(" - ", 1)[0]
    if "," in name:
        return name.rsplit(",", 1)[1].strip()
    return name.strip()


def _load_stations():
    with open(LOCATIONS_FILE) as f:
        raw = json.load(f)

    stations = []
    for loc in raw.get("results", []):
        coords = loc.get("coordinates") or {}
        lat    = coords.get("latitude")
        lng    = coords.get("longitude")
        if lat is None or lng is None:
            continue

        sensors    = loc.get("sensors", [])
        pollutants = list({s["parameter"]["name"] for s in sensors})
        sensor_map = {
            s["parameter"]["name"]: s["id"]
            for s in sensors
            if s["parameter"]["units"] == "µg/m³"   # prefer mass units
        }

        stations.append({
            "id":         loc["id"],
            "name":       loc["name"],
            "city":       _parse_city(loc["name"]),
            "provider":   loc.get("provider", {}).get("name", ""),
            "lat":        lat,
            "lng":        lng,
            "pollutants": sorted(pollutants),
            "sensor_map": sensor_map,   # {"pm25": sensor_id, "no2": sensor_id, ...}
        })

    return stations


STATIONS = _load_stations()
STATION_INDEX = {s["id"]: s for s in STATIONS}


# ---------------------------------------------------------------------------
# Helper: fetch latest sensor readings for one station (cached 1 hour)
# ---------------------------------------------------------------------------
def _get_latest(station_id: int):
    cached = cache.get(f"latest:{station_id}")
    if cached:
        return cached

    try:
        data    = openaq.get_location_sensors(station_id)
        sensors = data.get("results", [])
    except Exception:
        return {}

    readings = {}
    for s in sensors:
        param  = s.get("parameter", {})
        name   = param.get("name")
        units  = param.get("units")
        latest = s.get("latest") or {}

        # Support both response shapes OpenAQ v3 has used:
        # Shape A (current): latest = {"value": 45.5, "datetime": {"local": "..."}}
        # Shape B (older):   latest = {"value": 45.5, "datetime": "2024-01-01T..."}
        value  = latest.get("value")
        dt_raw = latest.get("datetime")
        if isinstance(dt_raw, dict):
            ts = dt_raw.get("local")
        elif isinstance(dt_raw, str):
            ts = dt_raw
        else:
            ts = None

        # Only keep µg/m³ units to avoid duplicates (ppm versions)
        if value is not None and units == "µg/m³" and name not in readings:
            readings[name] = {"value": round(value, 2), "units": units, "timestamp": ts}

    cache.set(f"latest:{station_id}", readings, ttl_seconds=3600)
    return readings


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def list_stations(
    pollutant: str = Query(None, description="Filter to stations that measure this pollutant (e.g. pm25)"),
    search:    str = Query(None, description="Search by station name or city"),
):
    """All India monitoring stations with coordinates and pollutant list."""
    result = STATIONS

    if pollutant:
        result = [s for s in result if pollutant in s["pollutants"]]

    if search:
        q = search.lower()
        result = [s for s in result if q in s["name"].lower() or q in s["city"].lower()]

    # Strip sensor_map from response (internal use only)
    return {
        "total": len(result),
        "stations": [
            {k: v for k, v in s.items() if k != "sensor_map"}
            for s in result
        ],
    }


@app.get("/api/stations/{station_id}/latest")
def station_latest(station_id: int):
    """Latest pollutant readings at a specific station."""
    station = STATION_INDEX.get(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    readings = _get_latest(station_id)
    return {
        "station_id":   station_id,
        "station_name": station["name"],
        "city":         station["city"],
        "lat":          station["lat"],
        "lng":          station["lng"],
        "readings":     readings,
    }


@app.get("/api/stations/{station_id}/trend")
def station_trend(
    station_id: int,
    pollutant:  str = Query("pm25", description="Pollutant name, e.g. pm25, no2, so2"),
    days:       int = Query(30, ge=1, le=365, description="Number of days of history"),
):
    """Daily trend for one pollutant at one station."""
    station = STATION_INDEX.get(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    sensor_id = station["sensor_map"].get(pollutant)
    if not sensor_id:
        raise HTTPException(
            status_code=404,
            detail=f"No {pollutant} sensor at this station. Available: {list(station['sensor_map'].keys())}"
        )

    cache_key = f"trend:{station_id}:{pollutant}:{days}"
    cached    = cache.get(cache_key)
    if cached:
        return cached

    try:
        data = openaq.get_sensor_daily(sensor_id, days_back=days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    series = []
    for r in data.get("results", []):
        period = r.get("period") or {}
        dt_from = period.get("datetimeFrom") or {}
        # Support both dict {"local": "..."} and plain string
        if isinstance(dt_from, dict):
            date = dt_from.get("local", "")[:10]
        elif isinstance(dt_from, str):
            date = dt_from[:10]
        else:
            date = ""
        value = r.get("value")
        if date and value is not None:
            series.append({"date": date, "value": round(value, 2)})

    series.sort(key=lambda x: x["date"])

    result = {
        "station_id":   station_id,
        "station_name": station["name"],
        "city":         station["city"],
        "pollutant":    pollutant,
        "units":        "µg/m³",
        "days":         days,
        "series":       series,
    }
    cache.set(cache_key, result, ttl_seconds=21600)   # 6 hours
    return result


@app.get("/api/top-polluted")
def top_polluted(
    pollutant: str = Query("pm25", description="Pollutant to rank by"),
    limit:     int = Query(10, ge=1, le=30),
):
    """Top N most polluted stations right now by a given pollutant.
    Uses a curated list of major city stations for speed.
    """
    # Curated major-city station IDs covering all regions of India (active stations)
    MAJOR_STATIONS = [
        8118, 5586, 5598, 6988, 10820,          # Delhi / NCR
        3409323, 3409510, 3409511, 3409513,     # Mumbai
        344103, 3409392,                         # Hyderabad, Vijayawada
        3409385, 3409387,                        # Bengaluru
        12046, 3409348,                          # Chennai, Madurai
        3409320, 3409524,                        # Kolkata / Howrah
        11610, 3409523,                          # Pune
        227533, 228301,                          # Ahmedabad, Gandhinagar
        3409430, 3409401,                        # Jaipur, Jodhpur
        227386, 228397,                          # Lucknow, Varanasi
        10914,                                   # Chandigarh
        3409390,                                 # Guwahati
    ]

    valid = [
        sid for sid in MAJOR_STATIONS
        if STATION_INDEX.get(sid) and pollutant in STATION_INDEX[sid]["pollutants"]
    ]

    def fetch_one(sid):
        readings = _get_latest(sid)
        reading  = readings.get(pollutant)
        if not reading:
            return None
        station = STATION_INDEX[sid]
        return {
            "station_id":   sid,
            "station_name": station["name"],
            "city":         station["city"],
            "lat":          station["lat"],
            "lng":          station["lng"],
            "value":        reading["value"],
            "units":        reading["units"],
            "timestamp":    reading["timestamp"],
        }

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_one, sid): sid for sid in valid}
        for future in as_completed(futures):
            item = future.result()
            if item:
                results.append(item)

    results.sort(key=lambda x: x["value"], reverse=True)
    return {
        "pollutant": pollutant,
        "ranked":    results[:limit],
    }


@app.get("/api/compare")
def compare_stations(
    ids:       str = Query(..., description="Comma-separated station IDs, e.g. 17,407,412"),
    pollutant: str = Query("pm25"),
    days:      int = Query(30, ge=1, le=90),
):
    """Compare trend data for multiple stations side by side."""
    try:
        station_ids = [int(x.strip()) for x in ids.split(",")]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")

    if len(station_ids) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 stations for comparison")

    out = []
    for sid in station_ids:
        station = STATION_INDEX.get(sid)
        if not station:
            continue
        sensor_id = station["sensor_map"].get(pollutant)
        if not sensor_id:
            continue

        cache_key = f"trend:{sid}:{pollutant}:{days}"
        cached    = cache.get(cache_key)
        if cached:
            out.append(cached)
            continue

        try:
            data = openaq.get_sensor_daily(sensor_id, days_back=days)
        except Exception:
            continue

        series = []
        for r in data.get("results", []):
            period = r.get("period") or {}
            dt_from = period.get("datetimeFrom") or {}
            if isinstance(dt_from, dict):
                date = dt_from.get("local", "")[:10]
            elif isinstance(dt_from, str):
                date = dt_from[:10]
            else:
                date = ""
            value = r.get("value")
            if date and value is not None:
                series.append({"date": date, "value": round(value, 2)})
        series.sort(key=lambda x: x["date"])

        result = {
            "station_id":   sid,
            "station_name": station["name"],
            "city":         station["city"],
            "pollutant":    pollutant,
            "units":        "µg/m³",
            "series":       series,
        }
        cache.set(cache_key, result, ttl_seconds=21600)
        if series:
            out.append(result)

    return {"pollutant": pollutant, "days": days, "stations": out}


@app.get("/api/health")
def health():
    return {"status": "ok", "stations_loaded": len(STATIONS)}


@app.get("/api/debug/openaq")
def debug_openaq():
    """Test live OpenAQ API call and return raw response for diagnosis."""
    import os
    api_key = os.getenv("OPENAQ_API_KEY")
    test_station = 8118  # New Delhi — always present

    result = {
        "api_key_set": bool(api_key),
        "api_key_prefix": api_key[:8] + "..." if api_key else None,
        "test_station_id": test_station,
        "raw_response": None,
        "error": None,
        "readings_parsed": None,
    }

    try:
        data    = openaq.get_location_sensors(test_station)
        sensors = data.get("results", [])
        result["raw_response"] = data
        result["sensor_count"] = len(sensors)

        readings = {}
        for s in sensors:
            param  = s.get("parameter", {})
            latest = s.get("latest") or {}
            readings[param.get("name")] = {
                "units":    param.get("units"),
                "value":    latest.get("value"),
                "datetime": (latest.get("datetime") or {}).get("local"),
                "has_latest_key": "latest" in s,
            }
        result["readings_parsed"] = readings

    except Exception as e:
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are an air quality analyst assistant embedded in the India Air Quality Explorer — a live dashboard \
built on CPCB (Central Pollution Control Board) data via OpenAQ.

WHAT THIS DASHBOARD SHOWS:
- Real-time and historical air quality data from 723 CPCB monitoring stations across India
- Pollutants: PM2.5, PM10, NO2, SO2, CO, O3 — all in µg/m³ (mass concentration)
- Color-coded AQI map of India, trend charts per station, city comparisons
- Data sourced from CAAQMS (Continuous Ambient Air Quality Monitoring System)

INDIA AQI BANDS (PM2.5 µg/m³, CPCB standard):
- 0–30: Good
- 31–60: Satisfactory
- 61–90: Moderate
- 91–120: Poor
- 121–250: Very Poor
- 250+: Severe
NAAQS 24-hr limits: PM2.5 = 60, PM10 = 100, NO2 = 80, SO2 = 80 (all µg/m³)
WHO 24-hr guideline: PM2.5 = 15 µg/m³ (India's limit is 4x more permissive)

KEY POLLUTANTS — what they are and where they come from:
- PM2.5: Fine particles under 2.5 microns. Primary health risk — penetrates lungs and bloodstream. Sources: vehicle exhaust, industrial emissions, crop burning.
- PM10: Coarse particles under 10 microns. Sources: dust, construction, roads.
- NO2: Nitrogen dioxide. Sources: vehicles, power plants. Causes respiratory illness.
- SO2: Sulphur dioxide. Sources: coal combustion, industrial processes. Acid rain precursor.
- CO: Carbon monoxide. Sources: incomplete combustion. Dangerous at high concentrations.
- O3: Ground-level ozone. Formed by chemical reactions between NOx and VOCs in sunlight.

INDIA CONTEXT:
- CPCB (Central Pollution Control Board) is under the Ministry of Environment, Forest and Climate Change (MoEFCC)
- CAAQMS network has 576+ real-time stations; OpenAQ aggregates this data
- Delhi consistently ranks among the world's most polluted capitals
- Major pollution drivers in India: vehicular emissions, thermal power plants, agricultural stubble burning (Oct–Nov in Punjab/Haryana), construction dust, industrial zones
- Crop burning season (Oct–Nov) causes severe AQI spikes in North India

ESG AND REGULATORY RELEVANCE:
- SEBI's BRSR (Business Responsibility and Sustainability Reporting) framework mandates India's top 1,000 listed companies to disclose environmental impact including air quality
- Principle 6 of BRSR specifically covers environmental disclosures
- Deloitte India's ESG advisory practice helps companies comply with BRSR and measure Scope 1/2/3 emissions
- CPCB data provides the baseline against which companies benchmark their environmental performance
- Companies near high-pollution stations face higher climate risk and ESG scrutiny

HOW TO USE THIS DASHBOARD (guide users to features):
- Click any marker on the map to see live readings and 30-day trend for that station
- Use "Top Polluted" button (top nav) to see the worst stations right now
- Use the pollutant dropdown to switch between PM2.5, PM10, NO2, SO2, CO, O3
- Search bar: type a city name (e.g. "Delhi", "Mumbai", "Bengaluru") to zoom to those stations
- Scroll down for national analytics: city rankings, pollutant coverage, and the station comparison tool
- Compare tool: pick up to 3 stations and overlay their 30-day PM2.5 trends

YOUR BEHAVIOR:
- You are a knowledgeable, direct assistant. No filler. No hedging.
- Answer questions about air quality, pollutants, CPCB data, India AQI, NAAQS, ESG/BRSR — in plain language.
- When relevant, guide the user to a specific feature of the dashboard.
- If asked about a specific city's current AQI: explain what that level means in plain terms.
- Keep responses concise — 2 to 4 sentences unless the question genuinely needs more depth.
- Do not invent specific numerical readings you do not have. If asked for real-time values, tell the user to check the dashboard map or click a station.
- No markdown formatting. Write in plain sentences only.
- No exclamation marks. No "Great question!" No "Happy to help!"
""".strip()


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]


@app.post("/api/chat")
async def chat(body: ChatRequest):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY not set")

    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization":  f"Bearer {api_key}",
                    "Content-Type":   "application/json",
                    "HTTP-Referer":   "http://localhost:3000",
                    "X-Title":        "India Air Quality Explorer",
                },
                json={
                    "model":      "anthropic/claude-sonnet-4-5",
                    "messages":   [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                    "max_tokens": 400,
                    "temperature": 0.4,
                },
            )
            resp.raise_for_status()
            data    = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return {"content": content}

        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
