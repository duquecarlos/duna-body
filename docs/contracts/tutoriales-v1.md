# tutoriales-v1 — how every Duna product teaches itself

Status: **v1, written by Main 2026-11-05** on Carlos's direction (typed in Main's session, 2026-11-05):

> *"this idea is about the tutorials that we have to make, some videos, maybe using some voices [...] even only text
> and recording screens [...] tutorials per chapter, and accessible through the same app, or through the same app and
> also at the onboarding processes [...] we record the real screen on the app and explain the process, and in every
> one of that we also do it like the agent way [...] and also talking about how to pay to Duna inside the app,
> everything [...] to kind of close the whole gap that is existing from here to onboarding a real user."*

And, minutes later: *"Obviously, that's a job, that part, it's a job for growth, or I don't know, [an agent] on its
own"* — so **production belongs to Growth** (`C:\Duna\growth`, whose charter already includes video production), not to
the product orchestrators. The split:

* **Growth produces:** the task scripts, the screen recordings, captions, narration and voice, the recorrido completo,
  and the media files. It drives each product's demo tenant; it writes no product code.
* **Each product provides:** the task inventory (from its own routes and agent tools), a demo tenant that a recording
  can drive, the catalog file, and the four doors (section 5). It never records or edits video itself.

Edgar's Tienda already ships the first instances (the panel's Ayuda answers «¿Cómo agrego un producto?» with a 1:38
video whose steps are its subtitles). This contract makes that the shape for every product, so a person who learns one
Duna app already knows how the next one teaches.

## 1. The unit is ONE TASK, not one product

A tutorial is one thing a person does: *registrar una venta*, *registrar una compra*, *anotar un costo*, *crear un
producto*, *confirmar un pedido*, *pagarle a Duna*. Short (target under 90 s). Carlos's open question — one whole
tutorial or one per chapter — is answered by having both without making both:

* **Task** — the unit. Recorded once.
* **Chapter** — an ordered list of tasks (*Ventas*, *Inventario*, *Tu cuenta y pagos*). Played back to back.
* **Recorrido completo** — the ordered list of chapters. The "whole tutorial" is a playlist, never a separate recording,
  so it can never disagree with its parts.

## 2. Every task is taught TWO ways, in the same tutorial

Every product has an agent (agent-ui-v1). So every task tutorial has two segments, in this order:

1. **En la app** — the real screens, navigated by hand.
2. **Con el asistente** — the same task said to the agent (typed or by voice), the agent's confirmation in the chat
   before it writes (agent-write-contract), and the same result visible in the app afterwards.

A task the agent cannot do yet is recorded with segment 1 only, and the catalog marks it, so the gap is a visible
list rather than a silence. A product with no agent today (the kitchen console, Cuenta, Pagos, Lugares) records
segment 1 only and says nothing about an agent; giving it one is a separate decision, not a tutorial gap. A task that exists ONLY through the agent is a defect of the product, not of the tutorial.

## 3. Tutorials are CODE, regenerated, never hand-recorded

A hand-recorded screen video is stale the first time the screen changes, and this team changes screens daily. So:

* Each task is a **script file kept by Growth** (`videos/tutoriales/<product>/<chapter>/<task>.ts` or `.json`): the
  steps as data — for each step, the on-screen action (a Playwright action against the demo tenant), the **caption**
  (the subtitle, always present), and the **narration** text (what a voice says, may equal the caption).
* Growth's generator drives the real app with Playwright, records the screen (`recordVideo`), writes the captions as
  WebVTT, and produces `<task>.webm`/`.mp4` plus a poster frame — one horizontal and one vertical (phone) render,
  since most users watch on a phone.
* **Re-recording is cheap and routine:** Growth re-runs every script against each product's main after a merge round,
  and a script that no longer finds its screen is a stale tutorial reported back to that product on the hub. Steps
  target stable accessible names (role + label), never CSS, so a restyle does not break them.
* Data comes from the product's demo/seed tenant, never a real customer's.

## 4. Voice is a LAYER, not a dependency

Narration is text in the script. Turning it into audio is a separate, swappable step:

* **Without voice:** captions only. Always shipped; this is the floor and it is complete on its own.
* **With voice:** a text-to-speech provider renders each step's narration and the generator lays it under the video.
  Whose voice (a neutral synthetic voice, or a voice built from recordings of real people, as Edgar is preparing for
  Tienda) is a per-product configuration, decided by Carlos. Using a real person's voice requires that person's
  recorded consent, kept with the voice samples.

Because voice is a layer, choosing or changing it re-renders every video without touching one script.

## 5. Where a person meets a tutorial — the same catalog, four doors

1. **Ayuda** — a help center in each app: chapters, their tasks, and the recorrido completo.
2. **In context** — a small «¿Cómo se hace?» on a screen opens THAT screen's task video in place (Tienda's
   PANEL-AYUDA-EN-SITIO-1 pattern), without leaving the work.
3. **Onboarding** — the first-run checklist (*Primeros pasos*) is a list of tasks; each step links its video and
   ticks itself when the person actually does the task.
4. **The agent** — asked *«¿cómo hago X?»*, the agent answers and links the task's video. The catalog is data the
   agent reads, so it never invents a step.

## 6. «Pagarle a Duna» is a chapter in EVERY product

How a business pays Duna — its plan, where the cobro arrives, paying it through duna-pagos, what happens when it is
late — is the same chapter in every product (*Tu cuenta y pagos*), recorded from that product's own entry point, so no
customer has to ask how to pay.

## 7. The catalog, and how a video reaches a product without a product slice

**Two files, two owners.** The PRODUCT owns what its tasks are; GROWTH owns which videos exist. Neither edits the
other's file, so a new video never needs a product slice and a renamed screen never needs Growth's permission.

**`tutoriales/catalogo.json` — in the product's repository, written by the product:**

```json
{
  "version": 1,
  "producto": "pedidos-dueno",
  "capitulos": [
    { "id": "ventas", "titulo": "Ventas", "orden": 1 }
  ],
  "tareas": [
    {
      "id": "registrar-pedido",
      "titulo": "Registrar un pedido",
      "capitulo": "ventas",
      "orden": 1,
      "pantalla": "/crear-pedido",
      "agente": "si",
      "onboarding": true
    }
  ]
}
```

* `producto` is the media folder name: `pedidos-dueno`, `pedidos-cocina`, `consola`, `cuenta`, `pagos`, `lugares`,
  `lotes`, `finanzas`, `tienda`.
* `agente`: `si` (the agent does it today), `no_aun` (the product has an agent that cannot do it yet), `no_aplica` (the
  product has no agent). Every task in a product with no agent is `no_aplica`.
* `pantalla` is the route the in-context door sits on; a census test keeps every `pantalla` a real route. A detail
  screen keeps its dynamic segment as written in the router (`/lugar/[id]`); its in-context door works there, and
  Ayuda's «Ir a la pantalla» links to `pantalla_lista` instead — an optional field naming the list the person picks
  an item from (`/mapa`). A task with a dynamic `pantalla` and no `pantalla_lista` gets no «Ir a la pantalla» link.
* Ids are kebab-case, stable forever: Growth's scripts and the agent link by id. A task that disappears is removed, its
  id is never reused.

**`manifest.json` — published by Growth next to the media, read by the product at runtime:**

    {TUTORIALES_BASE_URL}/{producto}/manifest.json
    { "version": 1, "producto": "...", "tareas": { "<id>": { "duracion_s": 84,
        "horizontal": "<url>", "vertical": "<url>", "subtitulos": "<url .vtt>", "poster": "<url>",
        "grabado_en": "<product commit sha>", "voz": "ninguna" } } }

* The base URL is ONE public env var per product (`NEXT_PUBLIC_TUTORIALES_BASE_URL`), documented in `.env.example`.
* A task in the catalog and not in the manifest shows **«Video en preparación»** — the task, its title and its screen
  link still render, so Ayuda is useful the day it ships, before any video exists.
* A manifest that cannot be fetched is treated as empty, never as an error screen.
* Hosting is Growth's call (a public bucket or static site); the product only needs a URL.

## 7b. Onboarding checklist completion is READ, never stored

A first-steps item is done when the product's own data says so (a first product exists, a first order was registered,
a passkey was added), computed at read time from data the product already has. No "done" flag, no new table, no
per-person progress row — so the checklist can never disagree with reality and needs no schema change. An item whose
completion cannot be read from existing data is not on the checklist.

## 8. What each product owes, in order

1. **Census (product)** — the product's real tasks (from its routes and its agent's tools), grouped into chapters,
   each marked app / agent / both, the first-run path a new customer walks, and where the doors would sit. Posted to
   Growth on the hub.
2. **Demo tenant and catalog (product)** — a seeded demo a recording can drive locally, and `tutoriales/catalogo.json`.
3. **Pilot (Growth)** — the generator and ONE task end to end with both segments, captions only.
4. **Doors (product)** — Ayuda, in-context, onboarding checklist, agent link, reading the catalog.
5. **Coverage (Growth)** — every onboarding task, then every task.
6. **Voice (Growth)** — when Carlos chooses it.

Products: pedidos (owner + operator consoles), consola, cuenta, pagos, lugares (Main); lotes, finanzas (their
orchestrators); tienda (Edgar — already started, this contract follows his shape rather than asking him to change it).
