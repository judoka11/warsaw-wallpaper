"""Route shapes and terminus points, built from the Warsaw GTFS feed."""
import csv
import io
import json
import logging
import os
import time
import zipfile
from collections import Counter
from pathlib import Path

import httpx

log = logging.getLogger("uvicorn.error")

GTFS_URL = os.getenv("GTFS_URL", "https://mkuran.pl/gtfs/warsaw.zip")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
ROUTES_FILE = DATA_DIR / "routes.geojson"
TERMINI_FILE = DATA_DIR / "termini.json"
METRO_FILE = DATA_DIR / "metro.json"
MAX_AGE_SECONDS = 7 * 24 * 3600
# GTFS route_type -> ours, trains (2) left out
ROUTE_TYPES = {"0": "tram", "1": "metro", "3": "bus"}


def is_stale() -> bool:
    if not all(f.exists() for f in (ROUTES_FILE, TERMINI_FILE, METRO_FILE)):
        return True
    return time.time() - ROUTES_FILE.stat().st_mtime > MAX_AGE_SECONDS


def load_termini() -> list[list[float]]:
    if not TERMINI_FILE.exists():
        return []
    return json.loads(TERMINI_FILE.read_text())


def load_routes() -> list[dict]:
    if not ROUTES_FILE.exists():
        return []
    return json.loads(ROUTES_FILE.read_text())["features"]


def load_metro() -> dict | None:
    if not METRO_FILE.exists():
        return None
    return json.loads(METRO_FILE.read_text())


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def _seconds(hms: str) -> int:
    # GTFS times can go past 24:00:00 for trips that run after midnight
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def _metro_timetable(zf: zipfile.ZipFile, routes: dict, trips: dict) -> dict:
    """The metro is one template trip per line, direction and day type, plus how often it
    repeats (frequencies.txt). Stops are stored as (seconds from departure, share of the route)."""
    stops: dict[str, list] = {trip_id: [] for trip_id in trips}
    for r in _rows(zf, "stop_times.txt"):
        if r["trip_id"] in stops:
            stops[r["trip_id"]].append((
                int(r["stop_sequence"]),
                _seconds(r["arrival_time"]),
                _seconds(r["departure_time"]),
                float(r["shape_dist_traveled"] or 0),
            ))

    windows: dict[str, list] = {trip_id: [] for trip_id in trips}
    for r in _rows(zf, "frequencies.txt"):
        if r["trip_id"] in windows:
            windows[r["trip_id"]].append([_seconds(r["start_time"]), _seconds(r["end_time"]), int(r["headway_secs"])])

    services = {t["service_id"] for t in trips.values()}
    dates: dict[str, list] = {}
    for r in _rows(zf, "calendar_dates.txt"):
        if r["service_id"] in services and r["exception_type"] == "1":
            dates.setdefault(r["date"], []).append(r["service_id"])

    timetable = []
    for trip_id, t in trips.items():
        rows = sorted(stops[trip_id])
        if len(rows) < 2 or not windows[trip_id]:
            continue
        start, length = rows[0][1], rows[-1][3] or 1
        times = []
        for _, arrival, departure, dist in rows:
            times.append([arrival - start, round(dist / length, 5)])
            if departure != arrival:
                times.append([departure - start, round(dist / length, 5)])
        timetable.append({
            "line": routes[t["route_id"]][0],
            "direction": t.get("direction_id", ""),
            "service": t["service_id"],
            "stops": times,
            "windows": sorted(windows[trip_id]),
        })
    return {"dates": dates, "trips": timetable}


def build_routes() -> int:
    """Downloads GTFS, writes routes.geojson, termini.json and metro.json, returns the number of shapes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DATA_DIR / "gtfs.zip"
    with httpx.stream("GET", GTFS_URL, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)

    with zipfile.ZipFile(zip_path) as zf:
        routes = {
            r["route_id"]: (r.get("route_short_name") or r["route_id"], ROUTE_TYPES[r["route_type"]])
            for r in _rows(zf, "routes.txt")
            if r["route_type"] in ROUTE_TYPES
        }

        # for each line and direction keep the shape most trips use
        usage: Counter = Counter()
        metro_trips = {}
        for t in _rows(zf, "trips.txt"):
            if t["route_id"] in routes and t.get("shape_id"):
                usage[(t["route_id"], t.get("direction_id", ""), t["shape_id"])] += 1
            if t["route_id"] in routes and routes[t["route_id"]][1] == "metro":
                metro_trips[t["trip_id"]] = t
        best: dict[tuple[str, str], tuple[str, int]] = {}
        for (route_id, direction, shape_id), n in usage.items():
            key = (route_id, direction)
            if key not in best or n > best[key][1]:
                best[key] = (shape_id, n)

        points: dict[str, list] = {shape_id: [] for shape_id, _ in best.values()}
        # termini are the ends of every shape, short and depot runs included
        firsts: dict[str, tuple] = {}
        lasts: dict[str, tuple] = {}
        used = {shape_id for _, _, shape_id in usage}
        for p in _rows(zf, "shapes.txt"):
            shape_id = p["shape_id"]
            if shape_id not in used:
                continue
            point = (
                int(p["shape_pt_sequence"]),
                round(float(p["shape_pt_lon"]), 5),
                round(float(p["shape_pt_lat"]), 5),
            )
            if shape_id not in firsts or point < firsts[shape_id]:
                firsts[shape_id] = point
            if shape_id not in lasts or point > lasts[shape_id]:
                lasts[shape_id] = point
            pts = points.get(shape_id)
            if pts is not None:
                pts.append(point)

        metro = _metro_timetable(zf, routes, metro_trips)
    METRO_FILE.write_text(json.dumps(metro))

    # rounding to ~100 m merges shapes that end at the same loop
    termini = sorted({
        (round(lon, 3), round(lat, 3))
        for _, lon, lat in list(firsts.values()) + list(lasts.values())
    })
    TERMINI_FILE.write_text(json.dumps(termini))

    features = []
    for (route_id, direction), (shape_id, _) in sorted(best.items()):
        coords = []
        for _, lon, lat in sorted(points[shape_id]):
            if not coords or coords[-1] != [lon, lat]:
                coords.append([lon, lat])
        if len(coords) < 2:
            continue
        line, kind = routes[route_id]
        features.append({
            "type": "Feature",
            "properties": {"line": line, "type": kind, "direction": direction},
            "geometry": {"type": "LineString", "coordinates": coords},
        })

    tmp = ROUTES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    tmp.replace(ROUTES_FILE)
    zip_path.unlink(missing_ok=True)
    return len(features)
