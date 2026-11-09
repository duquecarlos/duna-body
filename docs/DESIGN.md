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
   chart of zeros. Empty states say what will appear and when.* Stale data says its own age once, above the figures
   it qualifies, not silently (BODY-HOY-STALE-DATA-1: threshold 2 days, since 0-1 is the normal morning before that
   night's sync). A screen whose whole section would be nothing but the same gap repeated shows that gap once, in
   the hero, and drops the empty section entirely (BODY-SHIFTS-REDUNDANT-EMPTY-TABLE-1); a section with at least one
   real figure stays, gaps and all -- a partial gap is still informative. A chart with no data points says so in its
   caption, never invites a tap or drag over nothing (BODY-GOAL-CHART-NO-CAPTION-1). Stale data is a flag, not a
   footnote (`.aviso`, amber tint and left bar, body-size ink text) -- the clearest "off the normal flow" state the
   app has gets more than muted `.note` grey under a giant score; amber, never red, and its link never points at a
   page that does not exist behind it (BODY-ESTADOS-VACIOS-2-M2). A chart's legend names only what it draws: an
   entry for a series with zero plotted points is omitted, not shown beside an empty line (BODY-ESTADOS-VACIOS-2-M2).
7. **Shifts are the axis.** *Days are banded by shift kind (bar + tint + word: Día, Noche, Descanso), on every
   time chart.*
8. **Simple by default, detail on demand** (Sencillo/Detallado). *Hoy shows 4 figures; "Ver todo" opens the rest.
   Plan presets come from your history ("De tus datos"); a value you change is marked "Tú" with reset.*
9. **Charts explain themselves** (Finanzas). *Legend, scale, value on tap, "Toca un día…". One colour per series.
   Card titles are questions (Paridad #33).*
10. **Plain Spanish, tú** (house voice). *Cuenta register (with «¿ ¡»). No "HRV" without a plain subtitle the first
    time ("variabilidad del pulso: más alto suele ser mejor descanso"). Units on every figure. Body speaks tú,
    everywhere, including the assistant's fixed answers; never usted (BODY-REGISTRO-TU-1). A link is never followed
    directly by punctuation -- a chat client can read it as part of the link. Both guarded by
    tests/test_registro_tu.py.*
11. **Phone first** (375 px, 44 px targets, bottom tabs; left rail ≥ 1024). Hover only under `(hover:hover)`;
    visible pressed and focus states; reduced motion respected (ui-interaccion-v1). *A target smaller than 44 px
    grows its hit area, never its drawing, with the `.hit` pattern (BODY-TAP-44-1): `position:relative` on the
    target and a borderless, see-through `::before`, inset to reach 44x44, in whatever space already surrounds
    it. A radio or checkbox counts as its full `label`, not the input alone. Exception: Tendencias' per-day chart
    anchors stay under 44 px -- the documented no-JS path (rule 9); with JS the chart's tap/drag readout is the
    target.*
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
    `default-src 'self'; script-src 'self'; style-src 'self';
    font-src 'self'; img-src 'self' data:; connect-src 'self'; manifest-src 'self';
    worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`, plus
    Cuenta's issuer origin in `form-action` once hosted (a `/logout`, `/privacidad/consentimiento` or
    `/privacidad/borrar` POST can 303-redirect there, via `cuenta.logout_url`, and Chrome applies `form-action`
    to that redirect, not only to the form's own `action=`). `style-src` carries no `'unsafe-inline'`
    (BODY-ESTILOS-1): no template or server-built markup (Python-built SVG included) ever carries a
    `style="..."` attribute -- spacing and layout go through the named utility classes in
    `body/static/body.css` (`.mt-8`, `.gap-10`, `.text-right`, ...), and the only colours any chart or
    swatch ever needs come from a fixed data-series palette of classes (`.c-body`, `.c-night`, `.stroke-body`,
    ...), never a `var(...)` string written into markup. A dynamic bar (a goal's progress, a day's sleep
    stages, a month's training segments) is an inline SVG `<rect>` sized by geometry attributes (`width=`,
    `height=`), which CSP does not treat as style, never by a `style="width:...%"` on a `<div>` or `<span>`.
    **The trap this guards against (BODY-ESTILOS-1-M):** an inline `style=` always won the cascade, however
    specific the class it was replacing was. A plain utility class does not -- CSS breaks a tie between two
    selectors of EQUAL specificity by source order (later wins), but a component rule written as a compound
    selector (`.plan li`, `.legend i`, `.est .fig`, two classes or a class+element) is MORE specific than a
    single-class utility REGARDLESS of order, so the utility loses even placed last in the file. A utility
    or modifier class meant to override such a rule must be written as a compound selector after it too
    (`.plan li.block`, `.legend i.legend-line`, `.est .fig.c-muted`) -- on specificity, not on position -- and
    an inline `<svg>` that replaces a block `<div>` needs its own `display:block; width:100%` (`svg.bar`,
    `svg.stage`), since an `<svg>` with no CSS width renders at its `viewBox` width instead of its container's.
    `tests/test_seguridad.py` guards the no-`style=` rule (templates, Python-built markup, static scripts) and
    that `style-src` carries no `'unsafe-inline'`. No page loads anything from a third-party origin
    (**BODY-FUENTES-PROPIAS-1**): Hanken Grotesk is self-hosted, two `@font-face` rules at the top of
    `body/static/body.css` (one per subset, `latin` and `latin-ext`), from `.woff2` files committed under
    `body/static/fonts/hanken-grotesk/` with their SIL Open Font License beside them -- the same files Duna's
    own console commits (`duna-admin-ui`, ADMIN-FUENTES-LOCALES-1). `body/web.py` registers `font/woff2` with
    `mimetypes` before the `/static` mount, since Python's `mimetypes` has no entry for `.woff2` on its own.
    `tests/test_fuentes_propias.py` guards all of this: each font file served as `font/woff2` and
    byte-identical to its sha256; no template or static file names `fonts.googleapis.com` or
    `fonts.gstatic.com`; the served policy's `style-src` and `font-src` carry no `https:` origin; the LICENSE
    ships beside the fonts. A new typeface comes in the same way: committed `.woff2` files plus their licence
    under `body/static/fonts/`, never fetched from a third party at request time.
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
    (BODY-RENDIMIENTO-1): a page view never writes, now guarded at the STATEMENT level, not only by content --
    `tests/_rutas.py`'s `contar_sql` traces every connection a request opens and counts write-verb statements
    (INSERT/UPDATE/DELETE/REPLACE/CREATE/ALTER/DROP, plus a COMMIT that followed one), so a write that leaves
    every row unchanged (BODY-SEGURIDAD-3's own `_dump` content-compare cannot see one) is still caught. The
    schema is made ready once per process per DB file, never on every request: `body/db.py`'s `ensure_ready(conn)`
    runs the real `init(conn)` (migrations, `CREATE ... IF NOT EXISTS`, the metric/person seed) the first time
    this process meets a given file -- keyed on the file's resolved path and inode, under a lock -- and is a
    no-op after that. Per-request code (`body/identity.py`, `body/rutas/comun.py`, `body/rutas/sesion.py`) calls
    `ensure_ready`, never `init` directly; `init` is unchanged and keeps its CLI callers (precios, demo, pull,
    shift_import, vida_export, vincular_cuenta), each already calling it once per process. Every GET route also
    carries a per-route cost ceiling on statements and connections (`tests/test_rendimiento.py`), measured on a
    populated fixture, so a route's own cost stays visible instead of hiding inside a near-constant per-request
    schema check. `body/db.py`'s `connect()` also sets `PRAGMA busy_timeout = 5000`, so a reader that collides
    with a concurrent writer (the daily pull, the assistant) waits up to 5s instead of failing immediately --
    it changes nothing in the database file, unlike a journal-mode change, which this slice deliberately leaves
    alone (BODY-RENDIMIENTO-WAL-1, not done).
    (BODY-RENDIMIENTO-2): a screen that needs one metric across many days reads that whole window ONCE, never
    once per day -- `body/lector.py`'s `Lector` loads it with one SELECT per metric, then answers
    `series`/`day_values` from memory with `body/db.py`'s own semantics. `readiness.compute` and
    `baselines.baseline` take it as an optional `lector=` keyword (default `None`, the original per-call
    queries, so every caller that does not loop over days is unchanged); the few call sites that do loop --
    `/shifts`' 56-day strip and `turnos_costo`'s block cost and recovery math -- build one `Lector` for their
    whole window and pass it through. The reader's own numbers are pinned equal to the pre-reader path by
    tests, never approximated.
    (BODY-RENDIMIENTO-3): a page is compressed by the app itself (`GZipMiddleware` in `body/web.py`, next to the
    routes and inside `SeguridadMiddleware`, so the fixed security-header set still lands on every response,
    compressed or not), so the LAN gets gzip too, not only the hosted copy behind Caddy's `encode gzip`. The
    assistant's streamed answer (`text/event-stream`) is excluded from compression and never buffered, by
    Starlette's own default exclude list, not a Body-specific rule. A `/static` file's own URL already carries
    a content hash (`static_url`'s `?v=`, rule above): a request whose `v` equals the CURRENT hash
    (`comun.STATIC_HASH`) gets `Cache-Control: public, max-age=31536000, immutable` -- that exact URL can only
    ever mean these bytes -- and any other `v` (absent, stale, or foreign) gets `no-cache`, revalidating with the
    ETag the mount already sends. The web app manifest (`manifest.webmanifest`) is a plain `/static` file like
    any other, loaded through `static_url` the same way, so it follows this same rule with no special case.
    `/sw.js` keeps `Cache-Control: no-cache` on purpose (it is not a hashed `/static` URL -- a stale service
    worker would keep serving an old cache name).
    (BODY-FUENTE-CACHE-1): a file under `/static/fonts/` gets the forever `Cache-Control` with or without
    `v`, because it is immutable by its path alone -- `tests/test_fuentes_propias.py` pins every file under
    `body/static/fonts/` by sha256, so an in-place change already fails the gate. This is what lets the
    self-hosted font (BODY-FUENTES-PROPIAS-1) get the same forever-cache as a versioned `/static` asset even
    though it carries no `v`: it is referenced from `body.css`'s `@font-face` `src:url(...)`, a static file,
    not a template, and `base.html`'s preload link must keep that exact same URL or the preload is wasted. A
    font update arrives under a new directory or file name, its hash pinned alongside the old one, with
    `body.css` and every preload link changed together to point at it.
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
18. **Accesibilidad estructural** (BODY-ACCESIBILIDAD-1). *Every page carries the structural basics a screen
    reader or a keyboard-only visitor needs: (a) `<html lang>` matches the person's language; (b) exactly one
    `<h1>`; (c) no duplicate `id`; (d) every `<a href>` and `<button>` has an accessible name (text content,
    `aria-label`, `aria-labelledby` pointing at an id that exists, or `title`; an `<svg>`/`<img>` child alone
    counts only if it carries its own `aria-label`/`alt`); (e) every `<input>` (except type hidden/submit/button),
    `<select>` and `<textarea>` is labelled (a `<label for>` matching its id, a wrapping `<label>`, or
    `aria-label`/`aria-labelledby`); (f) every `<img>` carries `alt` (empty allowed for decoration); (g) every
    inline `<svg>` carries `role="img"` plus an accessible name, or `aria-hidden="true"`; (h) no positive
    `tabindex`.* `tests/test_accesibilidad.py` sweeps every page this slice could reach (the populated fixture and
    the ESTADOS-VACIOS-1 sparse states A/D/E, each in es and en, local mode, plus hosted mode for the hosted-only
    pages) and guards all eight rules on every one of them, failing once with every offender listed. Tap-target
    size (rule 11's 44 px) is a layout measurement made in a real browser, not a structural one this test can see
    in server-rendered HTML -- the orchestrator checks it separately.

19. **Pantallas anchas** (ORCH-CARLOS-DELEGATES-TIER-1-1, Carlos 2026-11-08: "para pantallas más anchas deberíamos
    tener la opción en la app de llenar todo el panel"). *Full width is the default at ≥1024 px: `.wrap` fills
    `main` instead of capping at 820 px. The extra width becomes COLUMNS of cards and charts (CSS grid,
    `minmax()` + `auto-fill`/`auto-fit`, so the column count follows the real width and the assistant panel,
    open or not, with no fixed breakpoint) -- never longer lines of prose: running text (`p`, "why" lines,
    explainer paragraphs) keeps `max-width:70ch` in both modes. "Lectura" is a per-device alternative, on the
    same mechanism as the theme (localStorage `body.ancho`, read before paint by `tema.js`, changed in Ajustes
    by `ajustes_tema.js`): a centred 820 px reading column, no columns. The phone layout and the 768 px band are
    unchanged; the assistant panel still takes its width from the content, never the other way round.*

## Look
Duna family tokens (`docs/research/03`): paper #F4F2ED, card #FCFBF8, ink #121212, hairlines, Hanken Grotesk with
tabular numerals, no shadows, light + dark. Duna family look **approved by Carlos 2026-11-06**. Body's own marks:
- product mark **#406F45** (calm green, Duna "ok") as the 8 px square / 3 px bar;
- shift bands: Noche **#3E5A73** (info blue-grey), Día amber tint **#FBE9C6** with amber bar, Descanso neutral inset.

## Round process (how Carlos designs)
Mock with realistic demo data → his notes → Se mantiene / Cambia / Nuevo list → build → he tests on his phone →
follow-ups become rules here → transferable ones go to the hub.
