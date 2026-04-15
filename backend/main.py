import json
import os
import time
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
# Station cache — load from WAQI on startup, persist to file for fast restarts
# ---------------------------------------------------------------------------
STATIONS_FILE = Path(__file__).parent.parent / "waqi_stations.json"
POLLUTANTS    = {"pm25", "pm10", "no2", "so2", "co", "o3"}


def _parse_city(name: str) -> str:
    """Best-effort city extraction from WAQI station names."""
    # WAQI names: "Anand Vihar, Delhi" / "Sirifort, Delhi - CPCB" / "IGI Airport"
    if " - " in name:
        name = name.rsplit(" - ", 1)[0]
    if "," in name:
        return name.rsplit(",", 1)[1].strip()
    return name.strip()


def _load_stations():
    # Use cached file if it's less than 6 hours old
    if STATIONS_FILE.exists():
        age = time.time() - STATIONS_FILE.stat().st_mtime
        if age < 21600:
            with open(STATIONS_FILE) as f:
                return json.load(f)

    # Fetch fresh from WAQI
    try:
        data = openaq.get_india_stations()
    except Exception as e:
        # Fall back to stale file rather than crash
        if STATIONS_FILE.exists():
            with open(STATIONS_FILE) as f:
                return json.load(f)
        raise RuntimeError(f"Could not load stations from WAQI: {e}")

    stations = []
    for s in data.get("data", []):
        uid = s.get("uid")
        lat = s.get("lat")
        lng = s.get("lng")
        name = (s.get("station") or {}).get("name", "")
        aqi_raw = s.get("aqi", "-")

        if not (uid and lat and lng and name):
            continue

        try:
            aqi = int(aqi_raw) if str(aqi_raw) not in ("-", "", "null") else None
        except (ValueError, TypeError):
            aqi = None

        stations.append({
            "id":         uid,
            "name":       name,
            "city":       _parse_city(name),
            "provider":   "WAQI/CPCB",
            "lat":        lat,
            "lng":        lng,
            "pollutants": ["pm25"],   # updated after first feed fetch; default covers most stations
            "aqi":        aqi,
        })

    # Persist to file
    try:
        with open(STATIONS_FILE, "w") as f:
            json.dump(stations, f)
    except Exception:
        pass

    return stations


STATIONS      = _load_stations()
STATION_INDEX = {s["id"]: s for s in STATIONS}


# ---------------------------------------------------------------------------
# Helper: fetch latest readings for one station (cached 1 hour)
# ---------------------------------------------------------------------------
def _get_latest(station_id: int):
    cached = cache.get(f"latest:{station_id}")
    if cached is not None:
        return cached

    try:
        data = openaq.get_station_feed(station_id)
        feed = data.get("data", {})
    except Exception:
        return {}

    iaqi     = feed.get("iaqi", {})
    time_obj = feed.get("time", {})
    ts       = time_obj.get("iso") or time_obj.get("s")

    # Update station's pollutants list in memory from live feed
    station = STATION_INDEX.get(station_id)
    if station:
        live_pollutants = sorted(k for k in iaqi if k in POLLUTANTS)
        if live_pollutants:
            station["pollutants"] = live_pollutants

    readings = {}
    for pollutant, info in iaqi.items():
        if pollutant not in POLLUTANTS:
            continue
        val = info.get("v")
        if val is not None:
            readings[pollutant] = {
                "value":     round(float(val), 2),
                "units":     "µg/m³",
                "timestamp": ts,
            }

    cache.set(f"latest:{station_id}", readings, ttl_seconds=3600)
    return readings


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def list_stations(
    pollutant: str = Query(None),
    search:    str = Query(None),
):
    """All India monitoring stations with coordinates and pollutant list."""
    result = STATIONS

    if pollutant:
        result = [s for s in result if pollutant in s["pollutants"]]

    if search:
        q = search.lower()
        result = [s for s in result if q in s["name"].lower() or q in s["city"].lower()]

    return {
        "total":    len(result),
        "stations": [{k: v for k, v in s.items() if k != "aqi"} for s in result],
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
    pollutant:  str = Query("pm25"),
    days:       int = Query(7, ge=1, le=30),
):
    """7-day PM2.5 forecast for one station (from WAQI forecast data)."""
    station = STATION_INDEX.get(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    cache_key = f"trend:{station_id}:{pollutant}"
    cached    = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        data = openaq.get_station_feed(station_id)
        feed = data.get("data", {})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    daily  = (feed.get("forecast") or {}).get("daily", {}).get(pollutant, [])
    series = []
    for entry in daily:
        day = entry.get("day")
        val = entry.get("avg")
        if day and val is not None:
            series.append({"date": day, "value": round(float(val), 2)})

    series.sort(key=lambda x: x["date"])

    result = {
        "station_id":   station_id,
        "station_name": station["name"],
        "city":         station["city"],
        "pollutant":    pollutant,
        "units":        "µg/m³",
        "days":         len(series),
        "series":       series,
    }
    cache.set(cache_key, result, ttl_seconds=21600)
    return result


@app.get("/api/top-polluted")
def top_polluted(
    pollutant: str = Query("pm25"),
    limit:     int = Query(15, ge=1, le=30),
):
    """Top N most polluted stations by PM2.5 (or selected pollutant)."""
    # Use overall AQI (available without extra API calls) to pick candidates,
    # then fetch live readings for the top 30 to get actual pollutant values.
    candidates = sorted(
        [s for s in STATIONS if s.get("aqi") is not None],
        key=lambda s: s["aqi"],
        reverse=True,
    )[:30]

    # Also include any already-cached stations that might be highly polluted
    cached_high = [
        s for s in STATIONS
        if s not in candidates and cache.get(f"latest:{s['id']}")
    ]
    candidates = candidates + cached_high[:10]

    def fetch_one(station):
        readings = _get_latest(station["id"])
        reading  = readings.get(pollutant)
        if not reading:
            return None
        return {
            "station_id":   station["id"],
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
        futures = {pool.submit(fetch_one, s): s for s in candidates}
        for future in as_completed(futures):
            item = future.result()
            if item:
                results.append(item)

    results.sort(key=lambda x: x["value"], reverse=True)
    return {"pollutant": pollutant, "ranked": results[:limit]}


@app.get("/api/compare")
def compare_stations(
    ids:       str = Query(...),
    pollutant: str = Query("pm25"),
    days:      int = Query(7, ge=1, le=30),
):
    """Compare forecast data for up to 5 stations."""
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

        cache_key = f"trend:{sid}:{pollutant}"
        cached    = cache.get(cache_key)
        if cached is not None:
            out.append(cached)
            continue

        try:
            data = openaq.get_station_feed(sid)
            feed = data.get("data", {})
        except Exception:
            continue

        daily  = (feed.get("forecast") or {}).get("daily", {}).get(pollutant, [])
        series = []
        for entry in daily:
            day = entry.get("day")
            val = entry.get("avg")
            if day and val is not None:
                series.append({"date": day, "value": round(float(val), 2)})
        series.sort(key=lambda x: x["date"])

        if not series:
            continue

        result = {
            "station_id":   sid,
            "station_name": station["name"],
            "city":         station["city"],
            "pollutant":    pollutant,
            "units":        "µg/m³",
            "series":       series,
        }
        cache.set(cache_key, result, ttl_seconds=21600)
        out.append(result)

    return {"pollutant": pollutant, "days": days, "stations": out}


@app.get("/api/health")
def health():
    return {"status": "ok", "stations_loaded": len(STATIONS)}


@app.get("/api/debug/waqi")
def debug_waqi():
    """Test live WAQI API call for diagnosis."""
    api_key = os.getenv("WAQI_API_KEY")

    result = {
        "api_key_set":    bool(api_key),
        "api_key_prefix": api_key[:8] + "..." if api_key else None,
        "stations_in_memory": len(STATIONS),
        "raw_response":  None,
        "error":         None,
        "readings":      None,
    }

    try:
        data = openaq.get_station_feed(7722)   # Delhi US Embassy — reliably active
        feed = data.get("data", {})
        result["raw_response"] = feed
        result["readings"] = {
            k: v for k, v in (feed.get("iaqi") or {}).items()
            if k in POLLUTANTS
        }
    except Exception as e:
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are an air quality analyst assistant embedded in the India Air Quality Explorer — a live dashboard \
built on CPCB (Central Pollution Control Board) data via the World Air Quality Index (WAQI).

WHAT THIS DASHBOARD SHOWS:
- Real-time and forecast air quality data from monitoring stations across India
- Pollutants: PM2.5, PM10, NO2, SO2, CO, O3 — all in µg/m³ (mass concentration)
- Color-coded AQI map of India, current readings per station, 7-day PM2.5 forecast
- Data sourced from CPCB's CAAQMS (Continuous Ambient Air Quality Monitoring System)

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
- Delhi consistently ranks among the world's most polluted capitals
- Major pollution drivers: vehicular emissions, thermal power plants, agricultural stubble burning (Oct–Nov), construction dust
- Crop burning season (Oct–Nov) causes severe AQI spikes in North India

ESG AND REGULATORY RELEVANCE:
- SEBI's BRSR framework mandates India's top 1,000 listed companies to disclose environmental impact
- CPCB data provides the baseline against which companies benchmark their environmental performance
- Companies near high-pollution stations face higher climate risk and ESG scrutiny

HOW TO USE THIS DASHBOARD:
- Click any marker on the map to see live readings and 7-day PM2.5 forecast
- Use the pollutant dropdown to switch between PM2.5, PM10, NO2, SO2, CO, O3
- Search bar: type a city name to zoom to those stations
- Scroll down for national analytics: city rankings, pollutant coverage, station comparison tool

YOUR BEHAVIOR:
- Direct, no filler, no hedging.
- Answer questions about air quality, pollutants, CPCB data, India AQI, NAAQS, ESG/BRSR.
- Keep responses concise — 2 to 4 sentences unless the question needs more depth.
- Do not invent specific numerical readings. If asked for real-time values, tell the user to check the map.
- No markdown formatting. Plain sentences only.
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
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "http://localhost:3000",
                    "X-Title":       "India Air Quality Explorer",
                },
                json={
                    "model":       "anthropic/claude-sonnet-4-5",
                    "messages":    [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                    "max_tokens":  400,
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
