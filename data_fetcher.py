import requests
import json
import time
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENAQ_API_KEY")
BASE_URL = "https://api.openaq.org/v3"
HEADERS = {"X-API-Key": API_KEY}


def get(endpoint, params={}):
    url = f"{BASE_URL}{endpoint}"
    response = requests.get(url, headers=HEADERS, params=params)
    if response.status_code == 429:
        print("Rate limit hit. Waiting 60 seconds...")
        time.sleep(60)
        return get(endpoint, params)
    response.raise_for_status()
    return response.json()


def get_india_locations(limit=1000):
    return get("/locations", params={"iso": "IN", "limit": limit, "page": 1})


def get_location_sensors(location_id):
    return get(f"/locations/{location_id}/sensors")


def get_sensor_daily(sensor_id, days_back=30):
    date_to = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return get(f"/sensors/{sensor_id}/measurements/daily", params={
        "datetime_from": date_from,
        "datetime_to": date_to,
        "limit": 1000
    })


def get_parameters():
    return get("/parameters", params={"parameter_type": "pollutant"})


if __name__ == "__main__":

    # 1. What pollutants are tracked?
    print("\n=== POLLUTANT PARAMETERS ===")
    params = get_parameters()
    for p in params.get("results", []):
        print(f"  ID: {p['id']} | {p['name']} | Units: {p['units']} | Display: {p.get('displayName', '')}")

    # 2. India stations — total count + first 20
    print("\n=== INDIA MONITORING STATIONS (first 20) ===")
    locations = get_india_locations(limit=20)
    meta = locations.get("meta", {})
    print(f"  Total stations in India: {meta.get('found', 'unknown')}\n")
    for loc in locations.get("results", []):
        city   = loc.get("locality", "Unknown")
        name   = loc.get("name", "")
        coords = loc.get("coordinates", {})
        sensors = loc.get("sensors", [])
        pollutants = [s.get("parameter", {}).get("name", "") for s in sensors]
        print(f"  [{loc['id']}] {name}")
        print(f"       City: {city} | Lat: {coords.get('latitude')} | Lng: {coords.get('longitude')}")
        print(f"       Pollutants: {pollutants}\n")

    # 3. Drill into one station — New Delhi (ID: 8118)
    print("\n=== SENSORS AT NEW DELHI STATION (ID: 8118) ===")
    sensors = get_location_sensors(8118)
    pm25_sensor_id = None
    for s in sensors.get("results", []):
        param  = s.get("parameter", {})
        latest = s.get("latest", {})
        first  = s.get("datetimeFirst", {})
        last   = s.get("datetimeLast", {})
        print(f"  Sensor ID: {s['id']} | Pollutant: {param.get('name')} ({param.get('units')})")
        print(f"       Latest value: {latest.get('value')} at {latest.get('datetime', {}).get('local', 'N/A')}")
        print(f"       Data from: {first.get('local', 'N/A')} → {last.get('local', 'N/A')}\n")
        if param.get("name") == "pm25" and pm25_sensor_id is None:
            pm25_sensor_id = s["id"]

    # 4. Pull 30 days of daily PM2.5 for that station
    if pm25_sensor_id:
        print(f"\n=== 30-DAY DAILY PM2.5 — SENSOR {pm25_sensor_id} ===")
        daily = get_sensor_daily(pm25_sensor_id, days_back=30)
        for reading in daily.get("results", [])[:15]:
            date  = reading.get("period", {}).get("datetimeFrom", {}).get("local", "N/A")
            value = reading.get("value")
            print(f"  {date[:10]}  →  PM2.5: {value} µg/m³")
    else:
        print("\n  No PM2.5 sensor found at station 8118.")

    # 5. Save all India locations to file
    print("\n=== SAVING india_locations.json ===")
    all_locs = get_india_locations(limit=1000)
    with open("india_locations.json", "w") as f:
        json.dump(all_locs, f, indent=2)
    print(f"  Saved. Total stations: {all_locs['meta'].get('found')}")
