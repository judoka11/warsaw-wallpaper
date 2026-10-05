# wwa-wallpaper

Live map of Warsaw's buses and trams that I use as my desktop wallpaper.

It pulls real-time vehicle positions from the city's open data API every 10 seconds and draws every line on a dark map of Warsaw:

- the more vehicles a line has running, the thicker it gets
- the faster they're moving on average, the brighter it is
- streaks flow along each line at its average speed (sped up 60×, real speed is invisible at city zoom)
- red rings: vehicles stuck in one place for 5+ minutes, not at a terminus
- magenta rings: bunching, i.e. two or more of the same line driving together

Metro has no live data, so M1 and M2 are just static lines.

Data: [Warsaw open data API](https://api.um.warszawa.pl) for positions, [Warsaw GTFS](https://mkuran.pl/gtfs/) for route shapes.

## Running it

You need Docker and a free API key from [api.um.warszawa.pl](https://api.um.warszawa.pl).

```sh
cp .env.example .env    # put your key in there
docker compose up --build -d
```

Then open <http://localhost:8000>. The first start takes about a minute because it downloads GTFS and builds the route shapes.

## As a wallpaper

I use [Lively Wallpaper](https://github.com/rocksdanister/lively) on Windows with this URL:

```
http://localhost:8000/?wallpaper=1
```

That hides the legend and locks the map. It fits the whole city to your screen by default; if you want a different view add something like `&zoom=11.5&lat=52.23&lon=21.01`.

## Code

- `backend/poller.py` polls the API and keeps 10 minutes of GPS history per vehicle
- `backend/checks.py` speeds, stalled vehicles, bunching
- `backend/routes.py` route shapes and termini from GTFS, rebuilt weekly
- `backend/main.py` FastAPI app: `/api/stats`, `/api/vehicles`, `/api/routes` and the page
- `frontend/index.html` the page itself, MapLibre plus a canvas for the streaks and rings

The API key only lives in `.env` on the backend, it never reaches the browser or the Docker image.
