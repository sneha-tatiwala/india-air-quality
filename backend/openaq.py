import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.getenv("WAQI_API_KEY")
BASE_URL = "https://api.waqi.info"


def _get(path, params=None, _retries=1):
    all_params = {"token": API_KEY, **(params or {})}
    resp = requests.get(f"{BASE_URL}{path}", params=all_params, timeout=15)
    if resp.status_code == 429:
        if _retries > 0:
            time.sleep(5)
            return _get(path, params, _retries - 1)
        raise Exception("WAQI rate limit reached. Try again later.")
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise Exception(f"WAQI error: {data.get('data', 'unknown error')}")
    return data


def get_india_stations():
    """All monitoring stations within India's geographic bounds."""
    return _get("/map/bounds/", {"latlng": "8,68,37,97"})


def get_station_feed(uid: int):
    """Current readings + 7-day forecast for one station by WAQI UID."""
    return _get(f"/feed/@{uid}/")
