"""Polls the Warsaw open data API for bus and tram positions."""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import checks

log = logging.getLogger("uvicorn.error")

API_URL = "https://api.um.warszawa.pl/api/action/busestrams_get/"
RESOURCE_ID = "f2e5503e-927d-4ad3-9500-4ab9e55deb59"
TYPES = {1: "bus", 2: "tram"}

API_KEY = os.getenv("ZTM_API_KEY", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
# the API happily returns vehicles that stopped reporting hours ago
MAX_AGE = timedelta(seconds=int(os.getenv("MAX_VEHICLE_AGE_SECONDS", "120")))
HISTORY = timedelta(minutes=10)
WARSAW = ZoneInfo("Europe/Warsaw")


class ApiError(Exception):
    pass


def _parse(record: dict, kind: str, now: datetime) -> dict | None:
    try:
        seen = datetime.strptime(record["Time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WARSAW)
        lat, lon = float(record["Lat"]), float(record["Lon"])
        line = str(record["Lines"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if now - seen > MAX_AGE:
        return None
    if not (51.9 < lat < 52.6 and 20.4 < lon < 21.7):
        return None
    return {
        "id": f"{kind}:{record.get('VehicleNumber')}",
        "line": line,
        "type": kind,
        "lat": lat,
        "lon": lon,
        "vehicle": record.get("VehicleNumber"),
        "brigade": record.get("Brigade"),
        "seen": seen,
        # set in _run_checks
        "speed": None,
        "heading": None,
        "stalled": False,
        "at_terminus": False,
        "direction": None,
    }


async def _fetch(client: httpx.AsyncClient, type_id: int, kind: str) -> list[dict]:
    resp = await client.get(
        API_URL,
        params={"resource_id": RESOURCE_ID, "apikey": API_KEY, "type": type_id},
    )
    resp.raise_for_status()
    result = resp.json().get("result")
    # errors come back as 200 with a string instead of a list
    if not isinstance(result, list):
        raise ApiError(str(result))
    now = datetime.now(WARSAW)
    return [v for r in result if (v := _parse(r, kind, now))]


class Poller:
    def __init__(self) -> None:
        self._by_type: dict[str, list[dict]] = {kind: [] for kind in TYPES.values()}
        self._tracks: dict[str, checks.Track] = {}
        self.updated: str | None = None
        # set from main once GTFS is loaded; the checks that need them wait until then
        self.termini: checks.Termini | None = None
        self.route_index: checks.RouteIndex | None = None
        self.bunches: list[dict] = []

    @property
    def vehicles(self) -> list[dict]:
        return [v for vehicles in self._by_type.values() for v in vehicles]

    def _run_checks(self) -> None:
        vehicles = self.vehicles
        for v in vehicles:
            track = self._tracks.setdefault(v["id"], [])
            fix = (v["seen"], v["lat"], v["lon"])
            # the same fix often comes back several polls in a row
            if not track or (fix[0] > track[-1][0] and not checks.is_glitch(track[-1], fix)):
                track.append(fix)
            while v["seen"] - track[0][0] > HISTORY:
                track.pop(0)
            v["speed"] = checks.speed_kmh(track)
            v["heading"] = checks.heading(track)
            if self.termini:
                v["stalled"] = checks.is_stalled(track, self.termini)
                v["at_terminus"] = self.termini.near(v["lat"], v["lon"])
            if self.route_index and v["heading"] is not None:
                v["direction"] = self.route_index.direction(v["line"], v["lat"], v["lon"], v["heading"])
        self.bunches = checks.find_bunches(vehicles, self.termini) if self.termini else []

        live = {v["id"] for v in vehicles}
        for vehicle_id in [i for i in self._tracks if i not in live]:
            del self._tracks[vehicle_id]

    async def run(self) -> None:
        if not API_KEY:
            log.error("ZTM_API_KEY is not set, nothing to fetch")
            return
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                for type_id, kind in TYPES.items():
                    try:
                        self._by_type[kind] = await _fetch(client, type_id, kind)
                        self.updated = datetime.now(WARSAW).isoformat()
                    except ApiError as e:
                        # the API fails a lot, just keep the last snapshot
                        log.warning("ZTM API error for %s: %s", kind, e)
                    except Exception as e:
                        # don't log the message, httpx puts the URL (with the key) in it
                        log.warning("ZTM fetch failed for %s: %s", kind, type(e).__name__)
                try:
                    self._run_checks()
                except Exception:
                    log.exception("Checks failed")
                await asyncio.sleep(POLL_SECONDS)
