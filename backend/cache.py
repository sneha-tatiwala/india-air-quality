import time

_store = {}

def get(key):
    entry = _store.get(key)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    return None

def set(key, data, ttl_seconds):
    _store[key] = {"data": data, "expires": time.time() + ttl_seconds}
