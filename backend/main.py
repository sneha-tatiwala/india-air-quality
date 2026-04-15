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
# Load stations from saved JSON (coordinates from CPCB via OpenAQ snapshot)
# Live readings are fetched from WAQI by geo-lookup at request time.
# ---------------------------------------------------------------------------
LOCATIONS_FILE = Path(__file__).parent.parent / "india_locations.json"
POLLUTANTS     = {"pm25", "pm10", "no2", "so2", "co", "o3"}


def _parse_city(name: str) -> str:
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
        pollutants = sorted({s["parameter"]["name"] for s in sensors})

        stations.append({
            "id":         loc["id"],
            "name":       loc["name"],
            "city":       _parse_city(loc["name"]),
            "provider":   (loc.get("provider") or {}).get("name", "CPCB"),
            "lat":        lat,
            "lng":        lng,
            "pollutants": pollutants,
        })

    return stations


STATIONS      = _load_stations()
STATION_INDEX = {s["id"]: s for s in STATIONS}


# ---------------------------------------------------------------------------
# Helper: fetch live readings for one station via WAQI geo-lookup (1-hr cache)
# ---------------------------------------------------------------------------
def _get_latest(station_id: int):
    cached = cache.get(f"latest:{station_id}")
    if cached is not None:
        return cached

    station = STATION_INDEX.get(station_id)
    if not station:
        return {}

    try:
        data = openaq.get_feed_by_geo(station["lat"], station["lng"])
        feed = data.get("data", {})
    except Exception:
        return {}

    iaqi     = feed.get("iaqi", {})
    time_obj = feed.get("time") or {}
    ts       = time_obj.get("iso") or time_obj.get("s")

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
    result = STATIONS

    if pollutant:
        result = [s for s in result if pollutant in s["pollutants"]]

    if search:
        q = search.lower()
        result = [s for s in result if q in s["name"].lower() or q in s["city"].lower()]

    return {"total": len(result), "stations": result}


@app.get("/api/stations/{station_id}/latest")
def station_latest(station_id: int):
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
    """7-day PM2.5 forecast from WAQI for the station's location."""
    station = STATION_INDEX.get(station_id)
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    cache_key = f"trend:{station_id}:{pollutant}"
    cached    = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        data = openaq.get_feed_by_geo(station["lat"], station["lng"])
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
    """Top N most polluted stations by current readings."""
    # Curated major-city station IDs (from india_locations.json)
    MAJOR_STATIONS = [
        8118, 5586, 5598, 6988, 10820,          # Delhi / NCR
        3409323, 3409510, 3409511, 3409513,      # Mumbai
        344103, 3409392,                          # Hyderabad, Vijayawada
        3409385, 3409387,                         # Bengaluru
        12046, 3409348,                           # Chennai, Madurai
        3409320, 3409524,                         # Kolkata / Howrah
        11610, 3409523,                           # Pune
        227533, 228301,                           # Ahmedabad, Gandhinagar
        3409430, 3409401,                         # Jaipur, Jodhpur
        227386, 228397,                           # Lucknow, Varanasi
        10914,                                    # Chandigarh
        3409390,                                  # Guwahati
    ]

    valid = [sid for sid in MAJOR_STATIONS if STATION_INDEX.get(sid)]

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
    return {"pollutant": pollutant, "ranked": results[:limit]}


@app.get("/api/compare")
def compare_stations(
    ids:       str = Query(...),
    pollutant: str = Query("pm25"),
    days:      int = Query(7, ge=1, le=30),
):
    """Compare 7-day forecast for up to 5 stations."""
    try:
        station_ids = [int(x.strip()) for x in ids.split(",")]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")

    if len(station_ids) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 stations")

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
            data = openaq.get_feed_by_geo(station["lat"], station["lng"])
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
    """Test live WAQI geo-lookup for diagnosis."""
    api_key = os.getenv("WAQI_API_KEY")

    result = {
        "api_key_set":        bool(api_key),
        "api_key_prefix":     api_key[:8] + "..." if api_key else None,
        "stations_in_memory": len(STATIONS),
        "raw_response":       None,
        "readings":           None,
        "error":              None,
    }

    # Test with coordinates of Anand Vihar, Delhi
    try:
        data = openaq.get_feed_by_geo(28.6469, 77.3152)
        feed = data.get("data", {})
        result["raw_response"] = {
            "station_name": (feed.get("city") or {}).get("name"),
            "aqi":          feed.get("aqi"),
            "time":         (feed.get("time") or {}).get("iso"),
        }
        result["readings"] = {
            k: v.get("v") for k, v in (feed.get("iaqi") or {}).items()
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
- Real-time and forecast air quality data from 292 CPCB monitoring stations across India
- Pollutants: PM2.5, PM10, NO2, SO2, CO, O3 — all in µg/m³
- Color-coded AQI map, current readings per station, 7-day PM2.5 forecast

INDIA AQI BANDS (PM2.5 µg/m³, CPCB standard):
- 0–30: Good | 31–60: Satisfactory | 61–90: Moderate | 91–120: Poor | 121–250: Very Poor | 250+: Severe
NAAQS 24-hr limits: PM2.5 = 60, PM10 = 100, NO2 = 80, SO2 = 80 (all µg/m³)
WHO 24-hr guideline: PM2.5 = 15 µg/m³

KEY POLLUTANTS:
- PM2.5: Fine particles, primary health risk. Sources: vehicles, industry, crop burning.
- PM10: Coarse particles. Sources: dust, construction.
- NO2: Nitrogen dioxide. Sources: vehicles, power plants.
- SO2: Sulphur dioxide. Sources: coal, industrial processes.
- CO: Carbon monoxide. Sources: incomplete combustion.
- O3: Ground-level ozone. Formed from NOx + VOCs in sunlight.

INDIA CONTEXT:
- Delhi consistently among world's most polluted capitals.
- Major drivers: vehicular emissions, thermal power plants, stubble burning (Oct–Nov), construction dust.

ESG RELEVANCE:
- SEBI BRSR framework: India's top 1,000 listed companies must disclose environmental impact.
- CPCB data is the benchmark for corporate ESG and climate risk assessment.

YOUR BEHAVIOR:
- Direct, no filler. 2–4 sentences unless more is genuinely needed.
- No markdown. No exclamation marks. No "Great question!"
- Don't invent readings — tell users to check the map for live values.
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
