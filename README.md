# Body app

Garmin health data on your own PC: daily numbers, trends, goals and plans, and (soon) your work shifts.
Health data and the Garmin token live in `BODY_HOME` (default `%USERPROFILE%\.body-app`), never in this repo.

## Start (Windows, PowerShell, in this folder)

```powershell
.\scripts\setup.ps1                        # once: virtual environment + libraries
.\.venv\Scripts\python -m body.login       # once: YOU sign in to Garmin (password hidden, never saved)
.\.venv\Scripts\python -m body.pull        # yesterday + today into the local database
.\.venv\Scripts\python -m body.web         # open http://127.0.0.1:8770
.\scripts\install-daily-sync.ps1           # optional: pull automatically every morning
```

Backfill: `python -m body.pull --days 90`. Phone at home: `python -m body.web --lan`, then open `http://<PC IP>:8770` on the phone.

## Layout

- `body/garmin_adapter.py`: the only file that knows the unofficial `python-garminconnect` library.
- `body/db.py`: SQLite schema. Metrics, sleep sessions, activities, **shifts**, goals, plan items and weekly checks are all data.
- `body/login.py`, `body/pull.py`, `body/web.py`: sign in, pull, page.
- Tests: `.venv\Scripts\python -m unittest discover tests -t .` (every test imports `tests/_isolate.py` first, so tests never touch real data) (needs `requirements-dev.txt`).
