"""Today's metro departures, worked out from the GTFS timetable (there's no live metro data)."""
from datetime import datetime, time, timedelta

DAY = 86400


def schedule(timetable: dict, now: datetime) -> dict:
    """Every departure for the day `now` falls in, in seconds since that day's midnight.

    Frequencies only say "every N seconds between A and B", so departures are placed
    at A, A+N, A+2N... which is a guess, but a good one. Trips that left yesterday
    and are still running after midnight are included with negative times.
    """
    today = now.date()
    days = [(today - timedelta(days=1), -DAY), (today, 0)]
    trips = []
    for trip in timetable["trips"]:
        duration = trip["stops"][-1][0]
        departures = []
        for day, shift in days:
            if trip["service"] not in timetable["dates"].get(day.strftime("%Y%m%d"), []):
                continue
            for start, end, headway in trip["windows"]:
                for t in range(start, end, headway):
                    if t + shift + duration > 0:
                        departures.append(t + shift)
        if departures:
            trips.append({
                "line": trip["line"],
                "direction": trip["direction"],
                "stops": trip["stops"],
                "departures": departures,
            })
    midnight = datetime.combine(today, time(), tzinfo=now.tzinfo)
    return {"day_start": int(midnight.timestamp() * 1000), "trips": trips}
