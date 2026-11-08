# Body app — design principles

Drawn on 2026-11-06 from Carlos's own rules across Duna (research in `docs/research/01–03`). Each rule says where it
comes from; the Body-specific reading is in *italics*. When a new rule arrives from the hub, add it here.

## The question each screen answers
| Screen | Question | Hero |
|---|---|---|
| **Hoy** | ¿Cómo llegas hoy y qué te toca? | **≈ Readiness** (Body's own score) + the shift ahead |
| **Turnos** | ¿Cuánto te cuesta una semana de noches y cuánto tardas en recuperarte? | ≈ recovery days after the last night block |
| **Tendencias** | ¿Vas mejorando? | each metric against *your own* 30-day baseline |
| **Metas** | ¿Tu plan está funcionando? | the weekly check, per goal |
| **Día** (drill-down) | ¿Qué pasó ese día? | sleep, shift, activity, every number of the day |

## Rules
1. **Data arrives by itself; you only confirm** (CARLOS-AUTO-INTAKE). *Garmin syncs each morning; shifts arrive from
   the Personal desk. Nothing is typed by hand except goals and a one-tap confirm.*
2. **Answer first, context one step away, never deleted** (Finanzas pulido). *One hero per screen; the rest opens.*
3. **Every figure opens what made it, with a way back** (Paridad #32, Lotes P2). *Resting HR → 30-day trend → the
   nights behind it → that day. Back always returns to the same spot (Paridad #30).* Exception (ORCH-CARLOS-DELEGATES-
   TIER-1-1, 2026-11-08): a figure another person shared with you (Hoy «Tu hogar») is never a link, because nothing
   of theirs sits behind it for you to open -- the row states its provenance in words instead (who shared it, that
   it came from their watch, and the day).
4. **Measured ≠ estimated** (Lotes/Finanzas confianza). *Watch figures solid ink. Derived ones ("días para
   recuperarte", "costo de la semana") soft, rounded, with «≈» and "cómo lo calculo".*
5. **Your baseline, not a population norm.** *Every comparison is "vs. tu promedio de 30 días" or "vs. tus semanas
   de día". No judgement colours from generic tables.*
6. **Honest gaps** (legibilidad, owner brief). *No watch that night → «—» + "sin datos del reloj". Never 0, never a
   chart of zeros. Empty states say what will appear and when.*
7. **Shifts are the axis.** *Days are banded by shift kind (bar + tint + word: Día, Noche, Descanso), on every
   time chart.*
8. **Simple by default, detail on demand** (Sencillo/Detallado). *Hoy shows 4 figures; "Ver todo" opens the rest.
   Plan presets come from your history ("De tus datos"); a value you change is marked "Tú" with reset.*
9. **Charts explain themselves** (Finanzas). *Legend, scale, value on tap, "Toca un día…". One colour per series.
   Card titles are questions (Paridad #33).*
10. **Plain Spanish, tú** (house voice). *Cuenta register (with «¿ ¡»). No "HRV" without a plain subtitle the first
    time ("variabilidad del pulso: más alto suele ser mejor descanso"). Units on every figure.*
11. **Phone first** (375 px, 44 px targets, bottom tabs; left rail ≥ 1024). Hover only under `(hover:hover)`;
    visible pressed and focus states; reduced motion respected (ui-interaccion-v1).
12. **Never medical advice.** *The app shows patterns in your own numbers and the plan you chose. It does not
    diagnose. Abnormal readings point to "habla con tu médico", nothing more.*
13. **Health data stays home** (Body CLAUDE.md). *Local DB; no values in URLs or logs; no third-party analytics;
    mocks use invented demo data.* Exception text from outside Body (a library's, the filesystem's, a parser's, the
    network's) is never shown, logged or stored -- the exception's TYPE name only, through `body/errores.py`
    (BODY-LOGIN-MENSAJES-1). (BODY-SEGURIDAD-1): every POST/PUT/PATCH/DELETE is same-origin only, checked by one
    middleware (`body/seguridad.py`); every response carries a fixed header set (`X-Content-Type-Options`,
    `Referrer-Policy`, `X-Frame-Options`, `Cross-Origin-Opener-Policy`) plus the Content-Security-Policy below;
    every redirect built from request data goes through `cuenta.safe_return_to`.
    (BODY-SEGURIDAD-2): no template holds an executable inline `<script>` or an inline event-handler attribute
    (`onchange=`, …) any more -- every one moved to `body/static/*.js`, loaded through `static_url(...)`, so
    `script-src 'self'` holds with no `'unsafe-inline'`. The full policy (`body/seguridad.py`'s
    `content_security_policy()`, built from named parts so a test can read each one):
    `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
    font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; manifest-src 'self';
    worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`, plus
    Cuenta's issuer origin in `form-action` once hosted (a `/logout`, `/privacidad/consentimiento` or
    `/privacidad/borrar` POST can 303-redirect there, via `cuenta.logout_url`, and Chrome applies `form-action`
    to that redirect, not only to the form's own `action=`). Remaining gap: `style-src` still carries
    `'unsafe-inline'` -- 22 templates (and server-built SVG) use `style="..."` attributes; untouched by this
    slice. Google Fonts (`fonts.googleapis.com`, `fonts.gstatic.com`) is the only third-party origin the policy
    allows, for Hanken Grotesk; self-hosting it instead is open as **BODY-FUENTES-PROPIAS-1**, not done here.
    (BODY-SEGURIDAD-4): any attempt that reaches a third party (Garmin) on a person's behalf is limited per
    person; the limit refuses before the third party is called, so Body's own server can never be used as a
    relay to try many credentials against it.
    (BODY-SEGURIDAD-3): a GET route never writes -- guarded by `tests/test_get_sin_escritura.py`, which sweeps
    every GET route against a populated database and fails if a single row changes (two routes, `/goals` and
    `/goals/{goal_id}`, broke this before by upserting a `goal_check` row through `goals.weekly_check` the
    first time anyone viewed a goal in a week, freezing that week's verdict at whoever opened it first).
    `goals.cerrar_semanas`, called once per person by the daily pull (`body/pull.py`, hosted and local), is the
    one writer of `goal_check`; it closes that person's current and previous week for every active goal they
    can see. The hosted daily pull closes weeks for every person who can see an active goal
    (`goals.personas_con_meta_activa`), not only the ones with a connected Garmin account: a habit goal's
    verdict never depends on Garmin data, so a household member with no Garmin connection, or whose pull
    failed that day, still gets theirs (BODY-SEGURIDAD-3-M). Screens compute the current week live, every
    time, with `goals.evaluar_semana` (pure, no write) and show it instead of any stored row for that same
    week.
14. **Assistant, to agent-ui-v1** (Carlos asked for it like the other apps, 2026-11-06) (one tap, 3 sizes, proposes → you confirm → undo). Built with parity: every
    screen ships with the assistant's read tool.

15. **Two languages from day one** (Carlos, 2026-11-06): Spanish (Colombia, tú, Cuenta register) is the default like
    every Duna app; **English** is a full version for Carlos. All copy lives in one dictionary per language; no
    string is written inside a screen. The language is a person setting.
16. **Readiness is ours, and it shows its work** (Carlos, 2026-11-06). Hero on Hoy is Body's own score, not Garmin's
    Body Battery. It is an estimate: «≈», rounded, a band word, and "cómo lo calculo" opens the parts (HRV vs normal,
    sleep vs need, Body Battery at wake, resting HR vs normal, yesterday's stress), each with its own number. With
    too little data it says so instead of guessing. Versioned (`body-v1`), so a formula change never rewrites the
    past silently.
17. **Connected to the other Duna apps** (Carlos, 2026-11-06). Where another app holds the answer, link to it
    instead of copying it (e.g. a night-shift week → its extra pay in Finanzas Personal). Body exposes a small
    summary other apps can read (readiness, sleep, shift). Same Cuenta identity when hosted.

## Look
Duna family tokens (`docs/research/03`): paper #F4F2ED, card #FCFBF8, ink #121212, hairlines, Hanken Grotesk with
tabular numerals, no shadows, light + dark. Duna family look **approved by Carlos 2026-11-06**. Body's own marks:
- product mark **#406F45** (calm green, Duna "ok") as the 8 px square / 3 px bar;
- shift bands: Noche **#3E5A73** (info blue-grey), Día amber tint **#FBE9C6** with amber bar, Descanso neutral inset.

## Round process (how Carlos designs)
Mock with realistic demo data → his notes → Se mantiene / Cambia / Nuevo list → build → he tests on his phone →
follow-ups become rules here → transferable ones go to the hub.
