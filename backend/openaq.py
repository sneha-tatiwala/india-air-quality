import requests
import time
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("OPENAQ_API_KEY")
BASE_URL = "https://api.openaq.org/v3"
HEADERS  = {"X-API-Key": API_KEY}


def _get(endpoint, params={}):
    url = f"{BASE_URL}{endpoint}"
    response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    if response.status_code == 429:
        time.sleep(60)
        return _get(endpoint, params)
    response.raise_for_status()
    return response.json()


def get_location_sensors(location_id: int):
    """Returns all sensors at a location, including latest reading."""
    return _get(f"/locations/{location_id}/sensors")


def get_sensor_daily(sensor_id: int, days_back: int = 30):
    """Returns daily aggregated measurements for a sensor."""
    now       = datetime.now(timezone.utc)
    date_to   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _get(f"/sensors/{sensor_id}/measurements/daily", params={
        "datetime_from": date_from,
        "datetime_to":   date_to,
        "limit":         days_back + 5,
    })
