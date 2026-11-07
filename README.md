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

## Keep it running

```powershell
.\scripts\install-daily-sync.ps1           # local only: web app at logon, daily pull, weekly file
.\scripts\install-daily-sync.ps1 -Lan      # same, plus the web app open to the home network
```

This registers three scheduled tasks: **Body app daily sync** pulls Garmin data every morning (and imports shift
files when `-ShiftsDir` is given), **Body app weekly file** writes the week's file for the Personal Sunday review,
and **Body app web** starts the web app at logon and restarts it if it stops, so it keeps running without a hand
step. It is local-only (`http://127.0.0.1:8770`) unless `-Lan` is passed.

## Body en el servidor: conectar este PC

When Body runs on the hosted server (`body.duna.solutions`) instead of this PC, this PC still uploads shift
files and fetches the weekly Body section for Personal -- through the device-key API, never by reading the
server's database directly.

```powershell
# 1. Mint a device key in the hosted app: Ajustes -> "Conexion del PC"
.\.venv\Scripts\python -m body.conectar_pc          # pastes the key, hidden; checks the connection
.\scripts\install-daily-sync.ps1 -Hosted -ShiftsDir "C:\path\to\your\shifts\folder"
```

`-Hosted` registers **Body app shift upload** (daily) and **Body app weekly fetch** (Sundays), and removes the
three local-mode tasks above, since the host now does both the pull and the web app. Forget the connection with
`python -m body.conectar_pc --olvidar`.

## Layout

- `body/garmin_adapter.py`: the only file that knows the unofficial `python-garminconnect` library.
- `body/db.py`: SQLite schema. Metrics, sleep sessions, activities, **shifts**, goals, plan items and weekly checks are all data.
- `body/login.py`, `body/pull.py`, `body/web.py`: sign in, pull, page.
- Tests: `.venv\Scripts\python -m unittest discover tests -t .` (every test imports `tests/_isolate.py` first, so tests never touch real data) (needs `requirements-dev.txt`).
