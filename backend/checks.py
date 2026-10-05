"""Speed, stalled vehicles and bunching, worked out from recent GPS fixes.

A track is a vehicle's recent fixes, oldest first, as (seen, lat, lon).
"""
import math
from collections import Counter, defaultdict
from datetime import datetime

Track = list[tuple[datetime, float, float]]

SPEED_WINDOW_S = 60
SPEED_MIN_SPAN_S = 15  # anything shorter is mostly GPS noise
SPEED_MAX_KMH = 120
# faster than this between two fixes is a glitch, unless it's a long jump (vehicle relocated)
GLITCH_KMH = 100
GLITCH_MAX_M = 1500
HEADING_MIN_M = 25
STALL_SECONDS = 300
STALL_RADIUS_M = 40
BUNCH_DISTANCE_M = 300
BUNCH_HEADING_DEG = 60
TERMINUS_RADIUS_M = 300

EARTH_RADIUS_M = 6371000


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # flat earth is plenty accurate within one city
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return EARTH_RADIUS_M * math.hypot(x, y)


class Termini:
    """Terminus points bucketed into a grid so lookups only check nearby ones."""

    # ~450 m cells, bigger than TERMINUS_RADIUS_M, so checking 3x3 cells is enough
    CELL_LAT = 0.004
    CELL_LON = 0.0065

    def __init__(self, points: list[list[float]]) -> None:
        self._cells: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
        for lon, lat in points:
            self._cells[self._cell(lat, lon)].append((lat, lon))

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return int(lat / self.CELL_LAT), int(lon / self.CELL_LON)

    def near(self, lat: float, lon: float) -> bool:
        row, col = self._cell(lat, lon)
        return any(
            distance_m(lat, lon, t_lat, t_lon) <= TERMINUS_RADIUS_M
            for r in (row - 1, row, row + 1)
            for c in (col - 1, col, col + 1)
            for t_lat, t_lon in self._cells.get((r, c), ())
        )


def line_counts(vehicles: list[dict]) -> dict[str, int]:
    return dict(Counter(v["line"] for v in vehicles).most_common())


def line_speeds(vehicles: list[dict]) -> dict[str, float]:
    by_line: dict[str, list[float]] = defaultdict(list)
    for v in vehicles:
        # skip layovers, otherwise every line looks slow
        if v["speed"] is not None and not v["at_terminus"]:
            by_line[v["line"]].append(v["speed"])
    return {line: round(sum(speeds) / len(speeds), 1) for line, speeds in by_line.items()}


def is_glitch(last: tuple[datetime, float, float], fix: tuple[datetime, float, float]) -> bool:
    span = (fix[0] - last[0]).total_seconds()
    distance = distance_m(last[1], last[2], fix[1], fix[2])
    if span <= 0 or distance > GLITCH_MAX_M:
        return False
    return distance / span * 3.6 > GLITCH_KMH


def speed_kmh(track: Track) -> float | None:
    seen, lat, lon = track[-1]
    for old_seen, old_lat, old_lon in track:
        span = (seen - old_seen).total_seconds()
        if span > SPEED_WINDOW_S:
            continue
        if span < SPEED_MIN_SPAN_S:
            return None
        speed = distance_m(old_lat, old_lon, lat, lon) / span * 3.6
        return round(speed, 1) if speed <= SPEED_MAX_KMH else None
    return None


def heading(track: Track) -> float | None:
    """Bearing of the last real movement, or None if the vehicle hasn't moved."""
    _, lat, lon = track[-1]
    for _, old_lat, old_lon in reversed(track[:-1]):
        if distance_m(old_lat, old_lon, lat, lon) >= HEADING_MIN_M:
            east = math.radians(lon - old_lon) * math.cos(math.radians(lat))
            north = math.radians(lat - old_lat)
            return math.degrees(math.atan2(east, north)) % 360
    return None


def is_stalled(track: Track, termini: Termini) -> bool:
    seen, lat, lon = track[-1]
    if (seen - track[0][0]).total_seconds() < STALL_SECONDS:
        return False
    for old_seen, old_lat, old_lon in track:
        if (seen - old_seen).total_seconds() > STALL_SECONDS:
            continue
        if distance_m(old_lat, old_lon, lat, lon) > STALL_RADIUS_M:
            return False
    return not termini.near(lat, lon)


def _same_way(a: float, b: float) -> bool:
    return abs((a - b + 180) % 360 - 180) <= BUNCH_HEADING_DEG


def find_bunches(vehicles: list[dict], termini: Termini) -> list[dict]:
    """Vehicles of the same line driving close together in the same direction."""
    by_line: dict[str, list[dict]] = defaultdict(list)
    for v in vehicles:
        if v["heading"] is not None:
            by_line[v["line"]].append(v)

    bunches = []
    for line, group in by_line.items():
        # union-find, so a chain like A-B, B-C ends up as one bunch
        parent = list(range(len(group)))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, a in enumerate(group):
            for j in range(i + 1, len(group)):
                b = group[j]
                if (
                    distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= BUNCH_DISTANCE_M
                    and _same_way(a["heading"], b["heading"])
                    and not termini.near(a["lat"], a["lon"])
                    and not termini.near(b["lat"], b["lon"])
                ):
                    parent[root(i)] = root(j)

        clusters: dict[int, list[dict]] = defaultdict(list)
        for i, v in enumerate(group):
            clusters[root(i)].append(v)
        for members in clusters.values():
            if len(members) < 2:
                continue
            bunches.append({
                "line": line,
                "type": members[0]["type"],
                "count": len(members),
                "lat": sum(v["lat"] for v in members) / len(members),
                "lon": sum(v["lon"] for v in members) / len(members),
            })
    return bunches
