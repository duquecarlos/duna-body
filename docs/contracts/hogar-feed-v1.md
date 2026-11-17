# hogar-feed-v1 — how Hogar reads Body

Body owns this contract (unlike the other files in this directory, which are read-only copies Main owns -- this one is
Body's own API, written for Hogar to check word for word). BODY-SERVICIO-HOGAR-1, ledger BODY-APPS-SIN-RESTRICCIONES-1.

## Carlos's ruling (dictated 2026-11-15 ~19:45, relayed verbatim by the Personal desk)

> «don't worry about the restriction between apps in body, finances, and [Hogar]. It's all me. It's all my own Duna
> servers, and I'm my own user, my own data. So, drop every restriction. Allow free communication between all of
> them.»

This replaces the per-unit person→app consent (BODY-COMPARTIR-APP-1, dropped) and the latest-value-only rule, for
Carlos's own data read by another Duna app on Duna's own host. Nothing below is a per-figure or per-unit gate: the
one standing grant is the `hogar:leer` device key itself.

## Base URL and transport

Body's own hosted origin (`body.duna.solutions` or equivalent). JSON over HTTPS.

## Auth

`Authorization: Bearer <device key>`, scope `hogar:leer`. The person mints the key on Body's own device-key page
(`/ajustes/dispositivos`) -- that act is the grant -- and revokes it there -- that act is the withdrawal. Hogar stops
reading the moment the key is revoked. No separate consent table, no per-figure or per-unit filter: the key carries
every figure, plan row and goal this person has.

A missing, unknown, revoked or wrong-scope key answers the same way every other device-key route in Body does:
* No `Authorization` header, or a key that does not resolve to a live row: `401 {"error": "no_autorizado"}`,
  `WWW-Authenticate: Bearer`.
* A live key without `hogar:leer` among its scopes: `403 {"error": "alcance"}`.

Both routes are read-only (GET), `Cache-Control: no-store`.

## `GET /api/hogar/manana?dia=YYYY-MM-DD`

`dia` defaults to today in the person's own timezone. Response:

```json
{
  "dia": "2026-11-16",
  "figuras": [{"metric_key": "resting_hr", "value": 58.0, "kind": "measured", "day": "2026-11-15"}],
  "plan": [{"id": 1, "day": "2026-11-16", "time": "18:00", "title": "Fuerza", "status": "proposed",
            "ejercicios": [{"ejercicio": "sentadilla", "series": 4, "reps": 10, "categoria": "pierna"}],
            "nota_sesion": null}],
  "horizonte": {"goal_id": 7, "title": "Media maratón", "deadline": "2027-02-01", "semana": 3, "semanas": 12},
  "turnos": [{"day": "2026-11-15", "kind": "night", "code": "N", "start_local": "2026-11-15T22:00:00",
              "end_local": "2026-11-16T06:00:00"}],
  "comidas": {
    "momentos": [{"clave": "desayuno", "desde_hora": 5, "hasta_hora": 11, "esperado": 1},
                 {"clave": "almuerzo", "desde_hora": 11, "hasta_hora": 16, "esperado": 1},
                 {"clave": "merienda", "desde_hora": 16, "hasta_hora": 19, "esperado": 0},
                 {"clave": "cena", "desde_hora": 19, "hasta_hora": 23, "esperado": 1}],
    "registradas": [{"momento": "desayuno", "ts_local": "2026-11-16T07:30:00"}],
    "faltan": ["almuerzo"]
  }
}
```

* `figuras`: every metric this person has, unfiltered -- never a selected list. Each metric_key's own LATEST stored
  day at or before `dia` (not a single shared day across metrics: a metric synced minutes ago and one not synced in
  days each show their own most recent value). `kind` is `measured` or `estimated` exactly as Body's own screens mark
  it (`readiness` is `estimated`).
* `plan`: every plan row for `dia` AND `dia`+1, any goal, unfiltered. `ejercicios` (BODY-HOGAR-EJERCICIOS-1) is the
  row's own planned exercise list, passed VERBATIM -- the exact items `planear_entreno` stored, no reshaping, no
  renaming, no added keys. Each item always carries `ejercicio` (the exercise name) and `series` (a count); `reps`
  and `categoria` are present only when the plan gave them. `null` when this plan row has no exercise list.
  `nota_sesion` (BODY-HOGAR-ESCRIBE-2) is the row's own note on how the session actually went, set by
  `anotar_sesion` below -- `null` when no one has noted it yet, so Hogar knows which planned sessions still lack
  one, and can ask about them, matched against the plan.
* `comidas` (BODY-HOGAR-ESCRIBE-2): what is missing today, no meal description, no nutrient.
  * `momentos`: this person's own meal moments, in order -- their own custom windows when they have any, Body's
    own defaults otherwise (`desayuno` 5-11, `almuerzo` 11-16, `merienda` 16-19, `cena` 19-23). `esperado` (0 or 1)
    marks a moment the person is expected to eat at (every default except `merienda`).
  * `registradas`: every meal already registered on `dia`, each with the moment the person stated when they
    registered it, or the one Body derives from its own time when they did not -- `null` when neither.
  * `faltan`: the claves of every `esperado` moment on `dia` with no registered meal. On a day before today, every
    one of them (the day is over); on a day after today, none (nothing is due yet); on today, only the ones whose
    `desde_hora` has already passed the current local hour.
* `horizonte`: the person's own active preparation goal (a milestone goal that is not itself a child of another
  plan) and which Monday-starting week `dia` falls in out of the horizon's whole span (`semana` = that week's
  1-based number, `semanas` = the horizon's total week count). `null` when this person has no active horizon.
* `turnos` (BODY-HOGAR-TURNOS-1): every shift that touches `dia` -- rows with `day` = `dia` or `dia`+1, plus a row
  with `day` = `dia`-1 whose `end_local` falls on `dia` or later (last night's shift still running this morning).
  Empty list when none, never omitted, never null. See "Turnos" below for the item shape.

## `GET /api/hogar/serie?metric_key=K&desde=YYYY-MM-DD&hasta=YYYY-MM-DD`

One metric's daily series over `[desde, hasta]`, at most 366 days per call.

```json
{"metric_key": "resting_hr", "puntos": [{"day": "2026-11-01", "value": 58.0, "kind": "measured"}]}
```

Errors: `desde`/`hasta` not a valid date, or `hasta` before `desde` -> `400`. The range spans more than 366 days ->
`400`. `metric_key` not a metric Body knows about -> `404`.

## Turnos (BODY-HOGAR-TURNOS-1)

One shift item, the same shape everywhere it appears in this contract -- the `shift` row's own column names, values
as stored (a null column stays null):

```json
{"day": "2026-11-15", "kind": "night", "code": "N", "start_local": "2026-11-15T22:00:00",
 "end_local": "2026-11-16T06:00:00"}
```

* `kind` is Body's own shift vocabulary, never the employer's own code: one of `morning`, `afternoon`, `night`,
  `day`, `off`, `other`.
* `code` is the schedule's own original code (e.g. `"N"`), or `null` when the shift carries none.
* `start_local`/`end_local` are ISO local datetimes (no timezone offset), or `null` on a shift with no recorded
  times (an `off` day). A night shift's `end_local` falls on the day after `start_local`.

Lists are ordered by `day`, then by `start_local` (a row with no `start_local` sorts before one that has one, on
the same day).

### `GET /api/hogar/turnos?desde=YYYY-MM-DD&hasta=YYYY-MM-DD`

Same `hogar:leer` dependency, same `Cache-Control: no-store`, same validation as `/api/hogar/serie`: `desde`/`hasta`
not a valid date, or `hasta` before `desde` -> `400`; a range over 366 days -> `400`. No shift in the range is not an
error -- an empty list.

```json
{"desde": "2026-11-01", "hasta": "2026-11-20", "turnos": [{"day": "2026-11-15", "kind": "night", "code": "N",
 "start_local": "2026-11-15T22:00:00", "end_local": "2026-11-16T06:00:00"}]}
```

Every shift row whose `day` is in `[desde, hasta]`, in the ordering above.

### Still v1

Both additions above are additive: `manana`'s four existing keys (`dia`, `figuras`, `plan`, `horizonte`) are
unchanged byte for byte except for `plan`'s own new `ejercicios` key, and `/api/hogar/serie` is untouched. Adding a
key to an existing response, or a new route, is not a breaking change to this contract. BODY-HOGAR-ESCRIBE-2's own
additions below (`manana`'s `comidas` key, `plan`'s `nota_sesion` key, and the two write routes) are additive too.

## Escribir (hogar:escribir)

A second, separate scope -- a person can grant `hogar:leer` without `hogar:escribir`, or either alone. Minted and
revoked on the same device-key page, same 401/403 shape as every other scope. Writes apply DIRECTLY (never a
pending state Hogar has to poll or re-confirm) and are undoable in Body exactly like a write the in-app assistant
made -- the Hoy screen's own card for it shows "Aplicado desde Hogar" and carries the usual Deshacer.

### `GET /api/hogar/herramientas`

The exact two tools Hogar may call, in the same schema shape Body's own `tools/list` already returns (`name`,
`description`, `inputSchema`) -- Hogar's own model fills the arguments from these schemas, as any assistant does;
this contract does not restate their shape, since this response is self-describing and the schema is Body's own to
change.

```json
{"herramientas": [{"name": "registrar_comida", "description": "...", "inputSchema": {"...": "..."}},
                   {"name": "anotar_sesion", "description": "...", "inputSchema": {"...": "..."}}]}
```

### `POST /api/hogar/registrar`

```json
{"herramienta": "registrar_comida", "argumentos": {"descripcion": "Arroz con pollo",
 "cantidades": [{"metric_key": "protein_g", "amount": 35}]}}
```

Runs that tool and applies the result immediately, tagged as Hogar's own write. Response:

```json
{"ok": true, "propuesta_id": 412, "resultado": {"propuesta_id": 412, "estado": "aplicada"}}
```

A refusal the tool itself raises (an unknown meal moment, a `plan_id` that is not this person's own session, a bad
amount) answers `200` -- it is not a transport error, it is the tool saying no -- with the sentence a person would
read for it:

```json
{"ok": false, "error": "momento desconocido: 'brunch'"}
```

`herramienta` outside the two names `/herramientas` lists -> `404`. A malformed body (not JSON, not an object) ->
`400`.

* `anotar_sesion`'s `plan_id` names one of `manana`'s own `plan` row ids -- NEVER a title: two sessions can share a
  title, only the id is this one planned session. A `plan_id` that does not resolve to this person's own plan row
  is refused the same way any other tool-level refusal is, by name.
* Two people in one household each have their OWN `hogar:escribir` key, under their own Cuenta Duna sign-in -- "two
  people ate dinner" is two separate `POST /api/hogar/registrar` calls, each under the eater's own key, never one
  call naming the other person.

## Conditions on the reader (not an app-to-app consent gate -- these protect people and services outside Carlos's
own data, and outside Duna)

1. **Other people's data.** This ruling covers Carlos's own data only. A household member's Body data still follows
   Body's own household rules and that person's own consent -- a different person's `hogar:leer` key only ever
   carries that person's own figures, plan and horizon, the same as every other device-key scope.
2. **Runs on Duna's own host.** The reader that calls these routes runs on Duna's infrastructure; nothing in this
   contract authorizes calling them from outside it.
3. **Vendor clouds: commands only.** Nothing this contract returns -- no figure, no plan title, no goal title -- goes
   to a third-party vendor cloud or voice service (Alexa, Google Home, Tuya/Smart Life, TP-Link cloud, Nabu Casa
   cloud speech). A home-automation command built from it (for example "dim the lights") may leave to a vendor
   cloud; a health figure may not.
4. **Voice, locally, is allowed with no separate consent.** Carlos, relayed by the desk 2026-11-15: «Yes, Voz en Casa
   [sí] for body, like I told you, I don't want any restriction, I want fully connection between everything because
   I'm gonna be the user and I want everything to be automated as much as possible.» Speaking a figure this contract
   returns aloud on the home's own speakers, in any room, through LOCAL speech (the home's own mini PC, e.g.
   Whisper/Piper) needs no separate voice consent. It must never route through Nabu Casa cloud speech, Alexa, Google
   or another vendor voice path (condition 3 again, said once more because voice is where it would first leak).
5. **Security hygiene, not a consent gate.** No health value in a URL or a log line; the device key itself is never
   logged.
6. **Stops on revoke.** The moment the `hogar:leer` key is revoked on Body's device-key page, these routes refuse
   it (`401`) like any other revoked device key -- there is no grace period.
