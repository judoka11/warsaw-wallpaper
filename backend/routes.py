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
MAX_AGE_SECONDS = 7 * 24 * 3600
# GTFS route_type -> ours, trains (2) left out
ROUTE_TYPES = {"0": "tram", "1": "metro", "3": "bus"}


def is_stale() -> bool:
    if not ROUTES_FILE.exists() or not TERMINI_FILE.exists():
        return True
    return time.time() - ROUTES_FILE.stat().st_mtime > MAX_AGE_SECONDS


def load_termini() -> list[list[float]]:
    if not TERMINI_FILE.exists():
        return []
    return json.loads(TERMINI_FILE.read_text())


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def build_routes() -> int:
    """Downloads GTFS, writes routes.geojson and termini.json, returns the number of shapes."""
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
        for t in _rows(zf, "trips.txt"):
            if t["route_id"] in routes and t.get("shape_id"):
                usage[(t["route_id"], t.get("direction_id", ""), t["shape_id"])] += 1
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
