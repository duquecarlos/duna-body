"""Pull Garmin data into the local database.

    python -m body.pull                 # yesterday and today
    python -m body.pull --date 2026-11-05
    python -m body.pull --days 90       # backfill the last N days
"""
import argparse
import sys
from datetime import date, timedelta

from . import db, garmin_adapter


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m body.pull")
    p.add_argument("--date", help="one day, YYYY-MM-DD")
    p.add_argument("--days", type=int, default=2, help="how many days back from today (default 2)")
    a = p.parse_args(argv)

    days = [a.date] if a.date else [(date.today() - timedelta(n)).isoformat() for n in range(a.days)]
    conn = db.connect()
    db.init(conn)
    for d in sorted(days):
        try:
            data = garmin_adapter.fetch_day(d)
        except garmin_adapter.NotSignedIn as e:
            print(e)
            return 2
        db.save_day(conn, data)
        missing = data.raw.get("_errors")
        # Counts only: health values never go to logs or stdout.
        print(f"{d}: {len(data.metrics)} metrics, {len(data.activities)} activities"
              + (f", missing feeds: {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
