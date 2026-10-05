import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import checks, metro, routes
from .poller import WARSAW, Poller

log = logging.getLogger("uvicorn.error")

FRONTEND = Path(__file__).parent.parent / "frontend"

poller = Poller()
metro_timetable: dict | None = None


def load_gtfs_data() -> None:
    global metro_timetable
    points = routes.load_termini()
    if points:
        poller.termini = checks.Termini(points)
    metro_timetable = routes.load_metro()


async def refresh_routes() -> None:
    while True:
        if routes.is_stale():
            try:
                log.info("Building routes from GTFS...")
                count = await asyncio.to_thread(routes.build_routes)
                log.info("Routes ready: %d shapes", count)
                load_gtfs_data()
            except Exception:
                log.exception("Building routes failed, will retry")
                await asyncio.sleep(300)
                continue
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_gtfs_data()
    tasks = [asyncio.create_task(poller.run()), asyncio.create_task(refresh_routes())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/vehicles")
def vehicles():
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "line": v["line"],
                    "type": v["type"],
                    "vehicle": v["vehicle"],
                    "speed": v["speed"],
                    "stalled": v["stalled"],
                },
                "geometry": {"type": "Point", "coordinates": [v["lon"], v["lat"]]},
            }
            for v in poller.vehicles
        ],
    }


@app.get("/api/routes")
def get_routes():
    # empty until the first GTFS build is done, the page keeps retrying
    if not routes.ROUTES_FILE.exists():
        return JSONResponse({"type": "FeatureCollection", "features": []})
    return FileResponse(routes.ROUTES_FILE, media_type="application/geo+json")


@app.get("/api/metro")
def get_metro():
    if not metro_timetable:
        return {"day_start": None, "trips": []}
    return metro.schedule(metro_timetable, datetime.now(WARSAW))


@app.get("/api/stats")
def stats():
    current = poller.vehicles
    return {
        "updated": poller.updated,
        "total": len(current),
        "counts": checks.line_counts(current),
        "speeds": checks.line_speeds(current),
        "stalled": [{"line": v["line"], "lat": v["lat"], "lon": v["lon"]} for v in current if v["stalled"]],
        "bunches": poller.bunches,
    }
