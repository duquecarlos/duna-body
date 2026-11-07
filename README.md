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

## Demo para tutoriales

Growth records each task's video against a seeded demo tenant, never against a real person's data
(tutoriales-v1 section 8). To run one:

```powershell
$env:BODY_HOME = "C:\path\to\a\scratch\folder"
.\.venv\Scripts\python -m body.demo --home $env:BODY_HOME --dias 90
$env:BODY_AGENTE_PROVEEDOR = "demo"
.\.venv\Scripts\python -m body.web
```

`body.demo` seeds one invented person ("Demo"), ~90 days of invented watch figures, a rotating shift
pattern with a night week, three goals with weekly checks and one habit -- always through the app's own
functions, with a fixed random seed so two runs give identical data. It **refuses** to touch a real data
folder: the default `BODY_HOME`, the current `BODY_HOME` environment value, the production host path, or
any folder that already holds a `body.db` it did not seed itself. The task catalog it drives is
`tutoriales/catalogo.json`.

## Asistente: tu clave de IA

El asistente responde con la clave de un proveedor (Anthropic, OpenAI o Gemini). La forma normal en este PC:
copia `.env.example` a `.env` en esta misma carpeta, pega tus claves ahi (solo se leen las lineas que
empiezan con `BODY_AGENTE_`) y reinicia la tarea "Body app web" (o vuelve a correr `python -m body.web`).
`.env` nunca se sube al repositorio (`.gitignore`); el servidor usa en cambio
`deploy/host/body.env.example` -> `/srv/duna/env/body.env`.

Alternativa para una sola sesion de PowerShell, sin tocar ningun archivo:

```powershell
$env:BODY_AGENTE_ANTHROPIC_API_KEY = "sk-..."
.\.venv\Scripts\python -m body.web
```

Una variable ya definida en tu entorno real (PowerShell, o tus variables de usuario de Windows) siempre gana
sobre lo que diga `.env`.

## Asistente: precios del modelo

The assistant's daily SPEND ceiling needs a price for the model in use; `body/agente/precios.json` holds a
versioned, cited seed for Duna's three standard models (`docs/contracts/modelos-ia-v1.md`: Anthropic
`claude-sonnet-5-5`, OpenAI `gpt-6.1-sol`, Gemini `gemini-3.8-flash`) and `body/agente/precios.py` fills
`precio_modelo` from it once per process, inserting only rows the table does not already have -- it never
overwrites a price an operator set by hand. Each row carries a `desde` date: a price applies from its date
until the next row's date for the same provider/model/type, which is how Gemini's own price step on
2027-01-01 is a data row, not a code change. Gemini's cached input has no published figure yet, so it is
priced at the full input price until Google's own page is read -- conservative, since it trips the spend
ceiling early rather than late. To correct a price, update that row directly (or run
`python -m body.agente.precios` again after editing `precios.json` for a fresh install); the seed never
undoes a hand correction. A model with no price anywhere falls back to the daily question-count limit
instead of the spend ceiling.

## Layout

- `body/garmin_adapter.py`: the only file that knows the unofficial `python-garminconnect` library.
- `body/db.py`: SQLite schema. Metrics, sleep sessions, activities, **shifts**, goals, plan items and weekly checks are all data.
- `body/login.py`, `body/pull.py`, `body/web.py`: sign in, pull, page.
- Tests: `.venv\Scripts\python -m unittest discover tests -t .` (every test imports `tests/_isolate.py` first, so tests never touch real data) (needs `requirements-dev.txt`).
